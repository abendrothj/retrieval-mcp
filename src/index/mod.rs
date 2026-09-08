use crate::{
    logging::now_ms,
    search::{
        lexical::{Page, pagination, validate_query},
        process,
    },
    source::{Workspace, excerpt},
};
use anyhow::{Context, Result, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
    time::{Duration, Instant},
};
use tree_sitter::{Node, ParseOptions, Parser};

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct SymbolArgs {
    /// Exact, case-sensitive definition name (for example parse_config). Rust and Python only.
    pub name: String,
    /// Optional repository-relative file or directory to restrict definitions.
    pub path: Option<String>,
    /// Maximum results, 1..100; default 20.
    pub limit: Option<usize>,
    /// Result offset, 0..10000; default 0.
    pub offset: Option<usize>,
}

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CallerArgs {
    /// Unqualified symbol name. Results are syntactic candidates, not a proven call graph.
    pub name: String,
    /// Optional repository-relative file or directory containing callers/references, not the target definition.
    pub path: Option<String>,
    /// Include possible identifier references as well as call sites. Defaults to false.
    pub include_references: Option<bool>,
    pub limit: Option<usize>,
    pub offset: Option<usize>,
}

#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct Symbol {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub path: String,
    pub line: usize,
    pub end_line: usize,
    pub container: Option<String>,
    pub excerpt: String,
}

#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct Reference {
    pub name: String,
    pub kind: String,
    pub path: String,
    pub line: usize,
    pub column: usize,
    pub caller: Option<String>,
    pub expression: String,
    pub excerpt: String,
}

#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct Import {
    pub path: String,
    pub line: usize,
    pub statement: String,
    pub candidate_files: Vec<String>,
    pub resolution: String,
}

#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct Coverage {
    pub snapshot_id: String,
    pub indexed_at_ms: u128,
    pub languages: Vec<String>,
    pub indexed_files: usize,
    pub unsupported_files: usize,
    pub skipped_files: usize,
    pub skipped_examples: Vec<String>,
    pub parse_error_files: usize,
    pub complete: bool,
    pub freshness: String,
    pub limitations: String,
}

#[derive(Serialize, JsonSchema)]
pub struct StructuralResult<T> {
    #[serde(flatten)]
    pub page: Page<T>,
    pub coverage: Coverage,
}

#[derive(Serialize, JsonSchema)]
pub struct CallerHit {
    #[serde(flatten)]
    pub reference: Reference,
    pub candidate_definitions: Vec<Symbol>,
    pub candidate_count: usize,
    pub candidates_truncated: bool,
    pub resolution: String,
    pub confidence: String,
}

#[derive(Serialize, JsonSchema)]
pub struct CallersResult {
    #[serde(flatten)]
    pub retrieval: StructuralResult<CallerHit>,
    pub imports: Vec<Import>,
    pub imports_truncated: bool,
    pub file_relationships: Vec<FileRelationship>,
    pub relationships_truncated: bool,
}

#[derive(Serialize, JsonSchema)]
pub struct FileRelationship {
    pub from: String,
    pub to: String,
    pub kind: String,
    pub resolution: String,
}

/// MCP tools depend on retrieval results, not the parser or storage representation.
pub trait StructuralBackend: Send + Sync {
    fn find_symbol(
        &self,
        workspace: &Workspace,
        args: SymbolArgs,
    ) -> Result<StructuralResult<Symbol>>;
    fn find_callers(&self, workspace: &Workspace, args: CallerArgs) -> Result<CallersResult>;
}

pub struct StructuralIndex {
    symbols: BTreeMap<String, Vec<Symbol>>,
    references: BTreeMap<String, Vec<Reference>>,
    imports: Vec<Import>,
    pub coverage: Coverage,
}

impl StructuralIndex {
    pub async fn build(workspace: Workspace, timeout: Duration) -> Result<Self> {
        let mut command = tokio::process::Command::new("rg");
        command.current_dir(workspace.root()).args([
            "--no-config",
            "--files",
            "--null",
            "--sort",
            "path",
            "--glob",
            "!.git/**",
            "--glob",
            "!target/**",
        ]);
        let (status, bytes) = process::run(&mut command, None, timeout, 2 * 1024 * 1024).await?;
        ensure!(
            status == 0 || status == 1,
            "cannot list repository files with ripgrep"
        );
        let files: Vec<String> = bytes
            .split(|b| *b == 0)
            .filter(|b| !b.is_empty())
            .map(|b| String::from_utf8(b.to_vec()).context("index requires UTF-8 file paths"))
            .collect::<Result<_>>()?;
        tokio::task::spawn_blocking(move || Self::from_files(&workspace, files, timeout)).await?
    }

    fn from_files(workspace: &Workspace, files: Vec<String>, timeout: Duration) -> Result<Self> {
        let timestamp = now_ms();
        let mut index = Self { symbols: BTreeMap::new(), references: BTreeMap::new(), imports: Vec::new(),
            coverage: Coverage { snapshot_id: timestamp.to_string(), indexed_at_ms: timestamp, languages: vec!["Rust".into(), "Python".into()],
                indexed_files: 0, unsupported_files: 0, skipped_files: 0, skipped_examples: Vec::new(), parse_error_files: 0,
                complete: false, freshness: "Full snapshot built on first structural call; restart the server after edits to rebuild.".into(),
                limitations: "Syntax only: no type checking, macro expansion, dynamic dispatch, alias/re-export or package resolution. Name matches are candidates, including when unique. Hidden/ignored files are excluded. Verify uncertain results with read_source.".into() } };
        let started = Instant::now();
        let mut total_bytes = 0;
        let mut total_records = 0;
        for file in files {
            if !matches!(
                Path::new(&file).extension().and_then(|s| s.to_str()),
                Some("rs" | "py")
            ) {
                index.coverage.unsupported_files += 1;
                continue;
            }
            // Bounded full snapshots suit small repositories; replace with incremental storage when these ceilings matter.
            if index.coverage.indexed_files >= 5000
                || total_bytes >= 32 * 1024 * 1024
                || total_records >= 200_000
                || started.elapsed() >= timeout
            {
                index.skip(&file, "snapshot budget reached");
                continue;
            }
            let source = match workspace.text(&file) {
                Ok(s) => s,
                Err(_) => {
                    index.skip(&file, "unreadable, oversized, binary, or unsafe path");
                    continue;
                }
            };
            total_bytes += source.len();
            let remaining = timeout.saturating_sub(started.elapsed());
            match parse_file(&file, &source, remaining) {
                Ok(parsed) => {
                    if parsed.has_error {
                        index.coverage.parse_error_files += 1;
                    }
                    total_records +=
                        parsed.symbols.len() + parsed.references.len() + parsed.imports.len();
                    for symbol in parsed.symbols {
                        index
                            .symbols
                            .entry(symbol.name.clone())
                            .or_default()
                            .push(symbol);
                    }
                    for reference in parsed.references {
                        index
                            .references
                            .entry(reference.name.clone())
                            .or_default()
                            .push(reference);
                    }
                    index.imports.extend(parsed.imports);
                    index.coverage.indexed_files += 1;
                }
                Err(_) => index.skip(&file, "parser timeout or per-file syntax budget exceeded"),
            }
        }
        for import in &mut index.imports {
            import
                .candidate_files
                .retain(|file| workspace.resolve(file).is_ok_and(|p| p.is_file()));
            if !import.candidate_files.is_empty() {
                import.resolution = "possible_local_module".into();
            }
        }
        Ok(index)
    }
    fn skip(&mut self, file: &str, reason: &str) {
        self.coverage.skipped_files += 1;
        if self.coverage.skipped_examples.len() < 10 {
            self.coverage
                .skipped_examples
                .push(format!("{file}: {reason}"));
        }
    }
}

fn scope(workspace: &Workspace, path: Option<&str>) -> Result<String> {
    workspace.relative(&workspace.resolve(path.unwrap_or("."))?)
}
fn in_scope(file: &str, path: &str) -> bool {
    path.is_empty() || Path::new(file).starts_with(path)
}
fn page<T: Clone>(items: impl Iterator<Item = T>, limit: usize, offset: usize) -> Page<T> {
    let mut results: Vec<_> = items.skip(offset).take(limit + 1).collect();
    let has_more = results.len() > limit;
    results.truncate(limit);
    Page {
        results,
        has_more,
        next_offset: has_more.then_some(offset + limit),
    }
}

impl StructuralBackend for StructuralIndex {
    fn find_symbol(
        &self,
        workspace: &Workspace,
        args: SymbolArgs,
    ) -> Result<StructuralResult<Symbol>> {
        validate_query(&args.name)?;
        let (limit, offset) = pagination(args.limit, args.offset)?;
        let scope = scope(workspace, args.path.as_deref())?;
        let definitions = self
            .symbols
            .get(&args.name)
            .into_iter()
            .flatten()
            .filter(|s| in_scope(&s.path, &scope))
            .cloned();
        Ok(StructuralResult {
            page: page(definitions, limit, offset),
            coverage: self.coverage.clone(),
        })
    }

    fn find_callers(&self, workspace: &Workspace, args: CallerArgs) -> Result<CallersResult> {
        validate_query(&args.name)?;
        let (limit, offset) = pagination(args.limit, args.offset)?;
        let scope = scope(workspace, args.path.as_deref())?;
        let candidates = self.symbols.get(&args.name).cloned().unwrap_or_default();
        let refs = self
            .references
            .get(&args.name)
            .into_iter()
            .flatten()
            .filter(|r| {
                in_scope(&r.path, &scope)
                    && (args.include_references.unwrap_or(false) || r.kind == "call")
            })
            .cloned();
        let refs = page(refs, limit, offset);
        let paths: BTreeSet<_> = refs.results.iter().map(|r| r.path.clone()).collect();
        let results: Vec<_> = refs
            .results
            .into_iter()
            .map(|reference| {
                let candidates: Vec<_> = candidates
                    .iter()
                    .filter(|s| {
                        Path::new(&s.path).extension() == Path::new(&reference.path).extension()
                    })
                    .cloned()
                    .collect();
                CallerHit {
                    reference,
                    candidate_definitions: candidates.iter().take(5).cloned().collect(),
                    candidate_count: candidates.len(),
                    candidates_truncated: candidates.len() > 5,
                    resolution: match candidates.len() {
                        0 => "unresolved",
                        1 => "unique_name_candidate",
                        _ => "ambiguous",
                    }
                    .into(),
                    confidence:
                        "low: spelling match only; receiver type and lexical binding are unresolved"
                            .into(),
                }
            })
            .collect();
        let mut imports: Vec<_> = self
            .imports
            .iter()
            .filter(|i| paths.contains(&i.path))
            .take(21)
            .cloned()
            .collect();
        let imports_truncated = imports.len() > 20;
        imports.truncate(20);
        let mut edges = BTreeSet::new();
        for hit in &results {
            for target in &hit.candidate_definitions {
                if target.path != hit.reference.path {
                    edges.insert((
                        hit.reference.path.clone(),
                        target.path.clone(),
                        "name_reference",
                    ));
                }
            }
        }
        for import in &imports {
            for target in &import.candidate_files {
                edges.insert((import.path.clone(), target.clone(), "import"));
            }
        }
        let relationships_truncated =
            edges.len() > 50 || results.iter().any(|h| h.candidates_truncated) || imports_truncated;
        let file_relationships = edges
            .into_iter()
            .take(50)
            .map(|(from, to, kind)| FileRelationship {
                from,
                to,
                kind: kind.into(),
                resolution: "candidate_only".into(),
            })
            .collect();
        Ok(CallersResult {
            retrieval: StructuralResult {
                page: Page {
                    results,
                    has_more: refs.has_more,
                    next_offset: refs.next_offset,
                },
                coverage: self.coverage.clone(),
            },
            imports,
            imports_truncated,
            file_relationships,
            relationships_truncated,
        })
    }
}

struct ParsedFile {
    symbols: Vec<Symbol>,
    references: Vec<Reference>,
    imports: Vec<Import>,
    has_error: bool,
}

fn node_text<'a>(node: Node<'_>, source: &'a str) -> &'a str {
    &source[node.byte_range()]
}
fn is_definition(kind: &str) -> bool {
    matches!(
        kind,
        "function_item"
            | "function_signature_item"
            | "struct_item"
            | "enum_item"
            | "trait_item"
            | "type_item"
            | "mod_item"
            | "const_item"
            | "static_item"
            | "function_definition"
            | "class_definition"
    )
}
fn is_import(kind: &str) -> bool {
    matches!(
        kind,
        "use_declaration" | "import_statement" | "import_from_statement"
    )
}

fn owner(mut node: Node<'_>, source: &str) -> Option<String> {
    while let Some(parent) = node.parent() {
        if (is_definition(parent.kind()) || parent.kind() == "impl_item")
            && let Some(name) = parent
                .child_by_field_name("name")
                .or_else(|| parent.child_by_field_name("type"))
        {
            return Some(excerpt(node_text(name, source), 200));
        }
        node = parent;
    }
    None
}

fn call_name(mut node: Node<'_>) -> Option<Node<'_>> {
    for _ in 0..32 {
        if matches!(
            node.kind(),
            "identifier" | "field_identifier" | "type_identifier"
        ) {
            return Some(node);
        }
        node = node
            .child_by_field_name("field")
            .or_else(|| node.child_by_field_name("attribute"))
            .or_else(|| node.child_by_field_name("name"))
            .or_else(|| node.child_by_field_name("function"))?;
    }
    None
}

fn parse_file(path: &str, source: &str, timeout: Duration) -> Result<ParsedFile> {
    let language = if path.ends_with(".rs") {
        tree_sitter_rust::LANGUAGE
    } else {
        tree_sitter_python::LANGUAGE
    };
    let mut parser = Parser::new();
    parser.set_language(&language.into())?;
    let start = Instant::now();
    let mut stop = |_: &tree_sitter::ParseState| {
        if start.elapsed() >= timeout {
            std::ops::ControlFlow::Break(())
        } else {
            std::ops::ControlFlow::Continue(())
        }
    };
    let options = ParseOptions::new().progress_callback(&mut stop);
    let tree = parser
        .parse_with_options(
            &mut |offset, _| &source.as_bytes()[offset..],
            None,
            Some(options),
        )
        .context("parse timed out")?;
    let mut parsed = ParsedFile {
        symbols: Vec::new(),
        references: Vec::new(),
        imports: Vec::new(),
        has_error: tree.root_node().has_error(),
    };
    let lines: Vec<_> = source.lines().collect();
    let mut definitions = BTreeSet::new();
    let mut call_names = BTreeSet::new();
    let mut nodes = Vec::new();
    let mut cursor = tree.walk();
    let mut depth = 0;
    loop {
        let node = cursor.node();
        if node.is_named() {
            nodes.push(node);
        }
        ensure!(
            nodes.len() <= 100_000 && depth <= 128 && start.elapsed() < timeout,
            "syntax budget exceeded"
        );
        if cursor.goto_first_child() {
            depth += 1;
            continue;
        }
        loop {
            if cursor.goto_next_sibling() {
                break;
            }
            if !cursor.goto_parent() {
                break;
            }
            depth -= 1;
        }
        if cursor.node() == tree.root_node() {
            break;
        }
    }
    for node in &nodes {
        ensure!(start.elapsed() < timeout, "syntax budget exceeded");
        if is_definition(node.kind())
            && let Some(name) = node.child_by_field_name("name")
        {
            definitions.insert(name.id());
            let line = node.start_position().row + 1;
            parsed.symbols.push(Symbol {
                id: format!("{path}:{line}:{}", name.start_position().column + 1),
                name: excerpt(node_text(name, source), 200),
                kind: node.kind().into(),
                path: path.into(),
                line,
                end_line: node.end_position().row + 1,
                container: owner(*node, source),
                excerpt: excerpt(lines.get(line - 1).unwrap_or(&""), 300),
            });
        }
        if matches!(node.kind(), "call_expression" | "call")
            && let Some(callee) = node.child_by_field_name("function")
            && let Some(name) = call_name(callee)
        {
            call_names.insert(name.id());
            parsed.references.push(reference(
                path,
                source,
                &lines,
                *node,
                name,
                "call",
                node_text(callee, source),
            ));
        }
        if is_import(node.kind())
            || (node.kind() == "mod_item" && node.child_by_field_name("body").is_none())
        {
            parsed.imports.push(import(path, source, *node));
        }
    }
    for node in nodes {
        ensure!(start.elapsed() < timeout, "syntax budget exceeded");
        if !matches!(
            node.kind(),
            "identifier" | "field_identifier" | "type_identifier"
        ) || definitions.contains(&node.id())
            || call_names.contains(&node.id())
        {
            continue;
        }
        let mut parent = node.parent();
        let mut excluded = false;
        while let Some(ancestor) = parent {
            if is_import(ancestor.kind())
                || matches!(
                    ancestor.kind(),
                    "parameters" | "parameter" | "type_parameters"
                )
            {
                excluded = true;
                break;
            }
            if is_definition(ancestor.kind()) {
                break;
            }
            parent = ancestor.parent();
        }
        if !excluded {
            parsed.references.push(reference(
                path,
                source,
                &lines,
                node,
                node,
                "possible_reference",
                node_text(node, source),
            ));
        }
    }
    parsed.references.sort_by_key(|r| (r.line, r.column));
    Ok(parsed)
}

fn reference(
    path: &str,
    source: &str,
    lines: &[&str],
    node: Node<'_>,
    name: Node<'_>,
    kind: &str,
    expression: &str,
) -> Reference {
    let line = node.start_position().row + 1;
    Reference {
        name: excerpt(node_text(name, source), 200),
        kind: kind.into(),
        path: path.into(),
        line,
        column: node.start_position().column + 1,
        caller: owner(node, source),
        expression: excerpt(expression, 200),
        excerpt: excerpt(lines.get(line - 1).unwrap_or(&""), 300),
    }
}

fn import(path: &str, source: &str, node: Node<'_>) -> Import {
    let mut candidates = Vec::new();
    let parent = Path::new(path).parent().unwrap_or(Path::new(""));
    if node.kind() == "mod_item"
        && let Some(name) = node.child_by_field_name("name")
    {
        let name = node_text(name, source);
        // Rust's sibling module convention only; #[path], inline modules, and crate layout remain unresolved.
        let base = if matches!(
            Path::new(path).file_name().and_then(|p| p.to_str()),
            Some("lib.rs" | "main.rs" | "mod.rs")
        ) {
            parent.to_path_buf()
        } else {
            parent.join(Path::new(path).file_stem().unwrap_or_default())
        };
        candidates.push(
            base.join(format!("{name}.rs"))
                .to_string_lossy()
                .into_owned(),
        );
        candidates.push(
            base.join(name)
                .join("mod.rs")
                .to_string_lossy()
                .into_owned(),
        );
    } else if node.kind() == "import_from_statement"
        && let Some(module) = node.child_by_field_name("module_name")
    {
        let name = node_text(module, source);
        if !name.starts_with('.') {
            let name = name.replace('.', "/");
            candidates.push(format!("{name}.py"));
            candidates.push(format!("{name}/__init__.py"));
        }
    } else if node.kind() == "import_statement" {
        let mut cursor = node.walk();
        for child in node.named_children(&mut cursor) {
            let child = child.child_by_field_name("name").unwrap_or(child);
            let name = node_text(child, source).replace('.', "/");
            candidates.push(format!("{name}.py"));
            candidates.push(format!("{name}/__init__.py"));
        }
    }
    Import {
        path: path.into(),
        line: node.start_position().row + 1,
        statement: excerpt(node_text(node, source), 300),
        candidate_files: candidates,
        resolution: "unresolved".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rust_and_python_definitions_calls_and_ambiguity() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("lib.rs"), "mod other;\nuse other::target;\nstruct Thing;\ntrait Run { fn run(&self); }\nfn target() {}\nimpl Thing { fn run(&self) { target(); self.run(); } }\nfn caller() { other::target(); }\n").unwrap();
        std::fs::write(dir.path().join("other.rs"), "pub fn target() {}\n").unwrap();
        std::fs::write(dir.path().join("app.py"), "from lib import target\nclass App:\n    def launch(self):\n        target()\n        self.unknown()\n").unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let index = StructuralIndex::from_files(
            &ws,
            vec!["lib.rs".into(), "other.rs".into(), "app.py".into()],
            Duration::from_secs(5),
        )
        .unwrap();
        let symbols = index
            .find_symbol(
                &ws,
                SymbolArgs {
                    name: "run".into(),
                    path: None,
                    limit: None,
                    offset: None,
                },
            )
            .unwrap();
        assert_eq!(symbols.page.results.len(), 2);
        assert!(
            symbols
                .page
                .results
                .iter()
                .any(|s| s.container.as_deref() == Some("Thing"))
        );
        let callers = index
            .find_callers(
                &ws,
                CallerArgs {
                    name: "target".into(),
                    path: None,
                    include_references: None,
                    limit: None,
                    offset: None,
                },
            )
            .unwrap();
        assert_eq!(callers.retrieval.page.results.len(), 3);
        assert!(callers.retrieval.page.results.iter().all(|r| {
            if r.reference.path.ends_with(".rs") {
                r.resolution == "ambiguous" && r.candidate_count == 2
            } else {
                r.resolution == "unresolved" && r.candidate_count == 0
            }
        }));
        assert!(
            callers
                .imports
                .iter()
                .any(|i| i.candidate_files.contains(&"other.rs".into()))
        );
        assert!(
            callers
                .file_relationships
                .iter()
                .all(|r| r.resolution == "candidate_only")
        );
        assert_eq!(index.coverage.indexed_files, 3);
        assert!(!index.coverage.complete);
        let unknown = index
            .find_callers(
                &ws,
                CallerArgs {
                    name: "unknown".into(),
                    path: None,
                    include_references: None,
                    limit: None,
                    offset: None,
                },
            )
            .unwrap();
        assert_eq!(unknown.retrieval.page.results[0].resolution, "unresolved");
        assert_eq!(
            unknown.retrieval.page.results[0]
                .reference
                .caller
                .as_deref(),
            Some("launch")
        );
    }
    #[test]
    fn comments_are_not_calls_and_broken_files_have_coverage() {
        let source = "fn real() {}\n// real();\nfn x() { let f = real; let s = \"real()\"; }\n";
        let parsed = parse_file("a.rs", source, Duration::from_secs(2)).unwrap();
        assert!(!parsed.references.iter().any(|r| r.kind == "call"));
        assert!(
            parsed
                .references
                .iter()
                .any(|r| r.name == "real" && r.kind == "possible_reference")
        );
        let broken = parse_file("bad.py", "def broken(\n", Duration::from_secs(2)).unwrap();
        assert!(broken.has_error);
        let nested = format!("{}fn deep() {{}}{}", "mod m {".repeat(130), "}".repeat(130));
        assert!(parse_file("deep.rs", &nested, Duration::from_secs(2)).is_err());
    }
}
