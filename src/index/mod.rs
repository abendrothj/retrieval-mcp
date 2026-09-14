use crate::{
    logging::now_ms,
    search::lexical::{Page, pagination, validate_query},
    source::{Workspace, excerpt},
};
use anyhow::{Context, Result, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    path::Path,
    time::{Duration, Instant},
};
use tree_sitter::{Node, ParseOptions, Parser};

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct SymbolArgs {
    /// Exact, case-sensitive definition name (for example parse_config).
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

#[derive(Clone, Copy, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TraceDirection {
    Callers,
    Callees,
}

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct TraceArgs {
    /// Unqualified function or method name at the root of the trace.
    pub name: String,
    /// Traverse callers (inbound) or callees (outbound). Defaults to callers.
    pub direction: Option<TraceDirection>,
    /// Maximum transitive call hops, 1..5; default 3.
    pub depth: Option<usize>,
    /// Optional repository-relative file or directory containing traversed call sites.
    pub path: Option<String>,
    /// Maximum returned edges, 1..100; default 20.
    pub limit: Option<usize>,
    /// Number of returned edges to skip, 0..10000; default 0.
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

/// Ceilings on one snapshot build, sized so that `--timeout-seconds` is the limit that normally
/// binds rather than these. They exist so a pathological repository cannot exhaust memory; they are
/// not a statement about what the tools can index. Measured cost is roughly 3 ms and 150 KB of
/// resident memory per indexed file, so the byte ceiling corresponds to something like 1.4 GB
/// resident in the worst case.
/// When one of them stops the build, `Coverage::budget_truncated` says so rather than leaving a
/// partial index indistinguishable from a complete one.
pub struct Budget;

impl Budget {
    pub const FILES: usize = 20_000;
    pub const BYTES: usize = 128 * 1024 * 1024;
    pub const RECORDS: usize = 2_000_000;
}

#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct Coverage {
    pub snapshot_id: String,
    pub indexed_at_ms: u128,
    pub languages: Vec<String>,
    pub indexed_files: usize,
    /// Supported-language files the walk enumerated, whether or not the budget allowed indexing
    /// them. `indexed_files` below this means the snapshot does not cover the repository.
    pub eligible_files: usize,
    pub unsupported_files: usize,
    pub skipped_files: usize,
    pub skipped_examples: Vec<String>,
    pub parse_error_files: usize,
    pub complete: bool,
    /// Construction stopped before reading every eligible file because a budget was exhausted.
    /// This is categorically different from `complete: false`, which is always true of syntactic
    /// resolution: here whole files were never scanned, so absence from a structural result is not
    /// evidence of absence anywhere in the repository.
    pub budget_truncated: bool,
    pub freshness: String,
    pub limitations: String,
}

#[derive(Serialize, JsonSchema)]
pub struct StructuralResult<T> {
    #[serde(flatten)]
    pub page: Page<T>,
    pub coverage: Coverage,
    /// "indexed" when the requested name is a known definition, "unknown_symbol" when it is not.
    /// An invented identifier must fail loudly: a silent empty page reads as a proven absence.
    pub symbol_status: String,
    /// Indexed names closest to an unrecognised request, so a wrong guess is recoverable.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub nearest_indexed_names: Vec<String>,
}

/// Which way a structural answer looked, and whether the other side of the symbol holds anything.
/// These are graph facts, not advice: the server states the shape and the caller decides.
#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct Orientation {
    pub symbol: String,
    pub incoming_callers: usize,
    pub outgoing_callees: usize,
    pub returned_relation: String,
    pub direction: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct InspectArgs {
    /// Unqualified symbol name to orient around.
    pub name: String,
    /// Optional repository-relative file or directory to restrict the neighbourhood.
    pub path: Option<String>,
}

/// One verified identity in a symbol's immediate neighbourhood, labelled by its relation.
#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct RelationRow {
    /// definition, inbound, outbound, or member.
    pub relation: String,
    pub symbol: String,
    pub kind: String,
    pub path: String,
    pub line: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub container: Option<String>,
}

#[derive(Serialize, JsonSchema)]
pub struct InspectResult {
    #[serde(flatten)]
    pub page: Page<RelationRow>,
    /// Full counts before the per-relation caps, so a truncated side is never read as empty.
    pub counts: BTreeMap<String, usize>,
    pub symbol_status: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub nearest_indexed_names: Vec<String>,
    pub coverage: Coverage,
    pub limitations: String,
}

/// The symbol a retrieved range falls inside, with honest call-graph salience.
#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct SymbolLocation {
    pub symbol: String,
    pub name: String,
    pub kind: String,
    pub path: String,
    pub line: usize,
    pub end_line: usize,
    /// Same-language call sites with this unqualified spelling. These are candidates for this
    /// definition, not resolved callers when several definitions share the name.
    pub name_candidate_callers: usize,
    /// Call expressions syntactically owned by this exact definition.
    pub direct_callees: usize,
}

/// One ranked candidate region, whatever ranker produced it.
#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct RankedRegion {
    pub path: String,
    pub start_line: usize,
    pub end_line: usize,
    pub score: f64,
}

/// BM25 over definition-shaped documents. No model, no service, no persistence: identifiers are
/// split on camelCase and underscores so a description's words reach a symbol's parts.
#[derive(Default)]
struct Bm25 {
    documents: Vec<RankedRegion>,
    lengths: Vec<usize>,
    postings: BTreeMap<String, Vec<(usize, usize)>>,
    total_length: usize,
}

pub fn concept_tokens(text: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    for word in text.split(|c: char| !c.is_alphanumeric()) {
        if word.is_empty() {
            continue;
        }
        let lowered = word.to_lowercase();
        // Sub-tokens let "wrap width" match wrapWidth and wrap_width without a stemmer.
        let mut part = String::new();
        let mut previous_lower = false;
        for character in word.chars() {
            if character.is_uppercase() && previous_lower && !part.is_empty() {
                tokens.push(std::mem::take(&mut part).to_lowercase());
            }
            previous_lower = character.is_lowercase() || character.is_numeric();
            part.push(character);
        }
        if !part.is_empty() {
            let part = part.to_lowercase();
            if part != lowered {
                tokens.push(part);
            }
        }
        tokens.push(lowered);
    }
    tokens
}

impl Bm25 {
    fn add(&mut self, region: RankedRegion, text: &str) {
        let tokens = concept_tokens(text);
        if tokens.is_empty() {
            return;
        }
        let document = self.documents.len();
        let mut counts: BTreeMap<String, usize> = BTreeMap::new();
        for token in &tokens {
            *counts.entry(token.clone()).or_default() += 1;
        }
        for (token, count) in counts {
            self.postings.entry(token).or_default().push((document, count));
        }
        self.total_length += tokens.len();
        self.lengths.push(tokens.len());
        self.documents.push(region);
    }

    fn search(&self, query: &str, scope: &str, wanted: usize) -> Vec<RankedRegion> {
        if self.documents.is_empty() {
            return Vec::new();
        }
        let average = self.total_length as f64 / self.documents.len() as f64;
        let (k1, b) = (1.2_f64, 0.75_f64);
        let mut scores: BTreeMap<usize, f64> = BTreeMap::new();
        for token in BTreeSet::from_iter(concept_tokens(query)) {
            let Some(postings) = self.postings.get(&token) else {
                continue;
            };
            let df = postings.len() as f64;
            let idf =
                (((self.documents.len() as f64 - df + 0.5) / (df + 0.5)) + 1.0).ln();
            for (document, frequency) in postings {
                let frequency = *frequency as f64;
                let normalized = 1.0 - b + b * (self.lengths[*document] as f64 / average);
                *scores.entry(*document).or_default() +=
                    idf * (frequency * (k1 + 1.0)) / (frequency + k1 * normalized);
            }
        }
        let mut ranked: Vec<_> = scores
            .into_iter()
            .filter(|(document, _)| in_scope(&self.documents[*document].path, scope))
            .collect();
        ranked.sort_by(|left, right| {
            right
                .1
                .partial_cmp(&left.1)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(left.0.cmp(&right.0))
        });
        ranked
            .into_iter()
            .take(wanted)
            .map(|(document, score)| RankedRegion {
                score,
                ..self.documents[document].clone()
            })
            .collect()
    }
}

#[derive(Serialize, JsonSchema)]
pub struct CallerHit {
    #[serde(flatten)]
    pub reference: Reference,
    pub candidate_count: usize,
    pub candidates_truncated: bool,
    pub resolution: String,
}

#[derive(Serialize, JsonSchema)]
pub struct CallersResult {
    #[serde(flatten)]
    pub retrieval: StructuralResult<CallerHit>,
    /// The definitions of the requested name. Identical for every row - they are the definitions
    /// of the name that was asked about - so they are stated once for the page rather than once per
    /// call site. Measured: 27-61% of a caller response was that repetition.
    pub candidate_definitions: Vec<Symbol>,
    /// Full definition count before the bounded page-level list.
    pub candidate_definition_count: usize,
    /// Whether `candidate_definitions` omits definitions after its first five entries.
    pub candidate_definitions_truncated: bool,
    /// What the rows are and are not, stated once for the page.
    pub confidence: String,
    pub imports: Vec<Import>,
    pub imports_truncated: bool,
    pub orientation: Orientation,
    pub file_relationships: Vec<FileRelationship>,
    pub relationships_truncated: bool,
}

#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct DependencyEdge {
    pub direction: TraceDirection,
    pub depth: usize,
    pub caller: String,
    pub callee: String,
    pub path: String,
    pub line: usize,
    pub expression: String,
    pub confidence: String,
}

#[derive(Serialize, JsonSchema)]
pub struct DependencyTraceResult {
    #[serde(flatten)]
    pub page: Page<DependencyEdge>,
    pub root_definitions: Vec<Symbol>,
    pub roots_truncated: bool,
    pub coverage: Coverage,
    pub symbol_status: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub nearest_indexed_names: Vec<String>,
    pub orientation: Orientation,
    pub limitations: String,
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
    /// Both sides of a symbol at one hop, capped, so direction never has to be guessed.
    fn inspect_symbol(&self, workspace: &Workspace, args: InspectArgs) -> Result<InspectResult>;
    fn find_symbol(
        &self,
        workspace: &Workspace,
        args: SymbolArgs,
    ) -> Result<StructuralResult<Symbol>>;
    fn find_callers(&self, workspace: &Workspace, args: CallerArgs) -> Result<CallersResult>;
    fn trace_dependencies(
        &self,
        workspace: &Workspace,
        args: TraceArgs,
    ) -> Result<DependencyTraceResult>;
    /// Rank definition-shaped regions for a natural-language description, without any model.
    fn search_concept(
        &self,
        workspace: &Workspace,
        query: &str,
        path: Option<&str>,
        wanted: usize,
    ) -> Result<Vec<RankedRegion>>;
    /// The innermost indexed definition containing the given line, if any.
    fn locate(&self, path: &str, line: usize) -> Option<SymbolLocation>;
    /// The snapshot's coverage, so callers can report corpus size alongside derived results.
    fn coverage(&self) -> &Coverage;
}

pub struct StructuralIndex {
    symbols: BTreeMap<String, Vec<Symbol>>,
    references: Vec<Reference>,
    references_by_name: BTreeMap<String, Vec<usize>>,
    calls_by_caller: BTreeMap<String, Vec<usize>>,
    imports: Vec<Import>,
    concepts: Bm25,
    pub coverage: Coverage,
}

impl StructuralIndex {
    pub async fn build(workspace: Workspace, timeout: Duration, no_ignore: bool) -> Result<Self> {
        // The same walk `search_exact` uses, so both describe one corpus: ripgrep's `ignore`
        // crate, honouring ignore files unless told otherwise, hidden files and `.git`/`target`
        // always excluded. No subprocess and no byte ceiling on the listing, so a repository large
        // enough to matter reaches the snapshot budget and reports `budget_truncated` rather than
        // failing on the size of its own file list.
        let listing = workspace.clone();
        let files = tokio::task::spawn_blocking(move || -> Result<Vec<String>> {
            let mut overrides = ignore::overrides::OverrideBuilder::new(listing.root());
            overrides.add("!.git/**")?;
            overrides.add("!target/**")?;
            let mut walk = ignore::WalkBuilder::new(listing.root());
            walk.overrides(overrides.build()?)
                .hidden(true)
                .sort_by_file_path(Path::cmp);
            if no_ignore {
                walk.ignore(false)
                    .git_ignore(false)
                    .git_global(false)
                    .git_exclude(false)
                    .parents(false);
            }
            let mut files = Vec::new();
            for entry in walk.build() {
                let entry = entry.context("cannot walk the repository")?;
                if !entry.file_type().is_some_and(|kind| kind.is_file()) {
                    continue;
                }
                if let Ok(relative) = listing.relative(entry.path()) {
                    files.push(relative);
                }
            }
            Ok(files)
        })
        .await??;
        tokio::task::spawn_blocking(move || Self::from_files(&workspace, files, timeout)).await?
    }

    fn from_files(workspace: &Workspace, files: Vec<String>, timeout: Duration) -> Result<Self> {
        let timestamp = now_ms();
        let mut index = Self {
            symbols: BTreeMap::new(),
            references: Vec::new(),
            references_by_name: BTreeMap::new(),
            calls_by_caller: BTreeMap::new(),
            imports: Vec::new(),
            concepts: Bm25::default(),
            coverage: Coverage {
                snapshot_id: timestamp.to_string(),
                indexed_at_ms: timestamp,
                languages: vec![
                    "Rust".into(),
                    "Python".into(),
                    "JavaScript/TypeScript".into(),
                    "Go".into(),
                    "Java".into(),
                    "C/C++".into(),
                ],
                indexed_files: 0,
                eligible_files: 0,
                unsupported_files: 0,
                skipped_files: 0,
                skipped_examples: Vec::new(),
                parse_error_files: 0,
                complete: false,
                budget_truncated: false,
                freshness: "Full snapshot built on first structural call; restart the server after edits to rebuild.".into(),
                limitations: "Syntax only: no type checking, macro expansion, dynamic dispatch, alias/re-export or package resolution. Name matches are candidates, including when unique. Hidden files and .git are excluded; ignore files are honored unless the server runs with --no-ignore. Verify uncertain results with read_source.".into(),
            },
        };
        let started = Instant::now();
        let mut total_bytes = 0;
        let mut total_records = 0;
        // Parsing is the expensive half of a snapshot and is embarrassingly parallel; merging is
        // not, because symbol and reference order is part of the answer. Files are read and parsed
        // a batch at a time across every available core and then merged in path order, so the
        // snapshot is what a single-threaded build produces and only the wall clock changes.
        let mut eligible = Vec::new();
        for file in files {
            if source_language(&file).is_some() {
                eligible.push(file);
            } else {
                index.coverage.unsupported_files += 1;
            }
        }
        index.coverage.eligible_files = eligible.len();
        let workers = std::thread::available_parallelism().map_or(1, |count| count.get());
        for batch in eligible.chunks(256) {
            // One deadline per batch: a per-file remainder would make the snapshot depend on the
            // order threads happened to finish in.
            let remaining = timeout.saturating_sub(started.elapsed());
            let parsed = parse_batch(workspace, batch, remaining, workers);
            for (file, outcome) in parsed {
                // Bounded full snapshots suit small repositories; replace with incremental storage
                // when these ceilings matter.
                if index.coverage.indexed_files >= Budget::FILES
                    || total_bytes >= Budget::BYTES
                    || total_records >= Budget::RECORDS
                    || started.elapsed() >= timeout
                {
                    // A file never scanned is not a file searched and found empty. Say so once
                    // here, rather than leaving the caller to infer it from a skip count.
                    index.coverage.budget_truncated = true;
                    index.skip(&file, "snapshot budget reached");
                    continue;
                }
                let Some((source, parsed)) = outcome else {
                    index.skip(&file, "unreadable, oversized, binary, or unsafe path");
                    continue;
                };
                total_bytes += source.len();
                match parsed {
                Ok(parsed) => {
                    if parsed.has_error {
                        index.coverage.parse_error_files += 1;
                    }
                    total_records +=
                        parsed.symbols.len() + parsed.references.len() + parsed.imports.len();
                    let lines: Vec<_> = source.lines().collect();
                    for symbol in parsed.symbols {
                        // Containers would re-index every member they hold; keep leaf
                        // definitions. A C++ namespace is the widest of them - one `namespace
                        // detail { ... }` can span a whole file - so it is excluded for the same
                        // reason a Rust `mod` is.
                        if !matches!(
                            symbol.kind.as_str(),
                            "mod_item" | "impl_item" | "namespace_definition"
                        ) {
                            let end = symbol.end_line.min(lines.len());
                            // Documentation sits above a definition, not inside it, and carries
                            // the vocabulary concept queries use. Include the contiguous comment
                            // and attribute block, bounded, in the chunk text; the reported
                            // region stays the definition itself.
                            let mut first = symbol.line - 1;
                            while first > 0 && symbol.line - 1 - first < 30 {
                                let above = lines[first - 1].trim_start();
                                if above.starts_with("//")
                                    || above.starts_with("/*")
                                    || above.starts_with('*')
                                    || above.starts_with('#')
                                    || above.starts_with("\"\"\"")
                                {
                                    first -= 1;
                                } else {
                                    break;
                                }
                            }
                            let body = lines[first..end].join("\n");
                            index.concepts.add(
                                RankedRegion {
                                    path: symbol.path.clone(),
                                    start_line: symbol.line,
                                    end_line: symbol.end_line,
                                    score: 0.0,
                                },
                                &format!(
                                    "{} {} {} {body}",
                                    symbol.path,
                                    symbol.container.clone().unwrap_or_default(),
                                    symbol.name
                                ),
                            );
                        }
                        index
                            .symbols
                            .entry(symbol.name.clone())
                            .or_default()
                            .push(symbol);
                    }
                    for reference in parsed.references {
                        let reference_index = index.references.len();
                        index
                            .references_by_name
                            .entry(reference.name.clone())
                            .or_default()
                            .push(reference_index);
                        if reference.kind == "call"
                            && let Some(caller) = &reference.caller
                        {
                            index
                                .calls_by_caller
                                .entry(caller.clone())
                                .or_default()
                                .push(reference_index);
                        }
                        index.references.push(reference);
                    }
                    index.imports.extend(parsed.imports);
                    index.coverage.indexed_files += 1;
                }
                    Err(_) => {
                        index.skip(&file, "parser timeout or per-file syntax budget exceeded")
                    }
                }
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
        if index.coverage.budget_truncated {
            // The standing limitations describe approximate *resolution*. Truncation is a
            // different claim - whole files were never read - so it is said in those terms, and
            // said where any tool result will carry it.
            index.coverage.limitations = format!(
                "{} Index construction stopped after {} of {} eligible files because its budget \
                 was exhausted, so this snapshot does not cover the repository: absence from a \
                 structural result is not evidence of absence. Narrow --root, or treat caller and \
                 dependency sets as partial.",
                index.coverage.limitations, index.coverage.indexed_files, index.coverage.eligible_files
            );
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

    /// Symbol spans for one already-read file, for callers that chunk source by definition.
    pub fn symbol_spans(path: &str, source: &str, timeout: Duration) -> Result<Vec<Symbol>> {
        Ok(parse_file(path, source, timeout)?.symbols)
    }

    fn trace_edges(
        &self,
        root: &str,
        direction: TraceDirection,
        scope: &str,
        max_depth: usize,
        max_results: usize,
    ) -> Vec<DependencyEdge> {
        let mut queue = VecDeque::from([(root.to_owned(), 0)]);
        let mut expanded = BTreeSet::new();
        let mut seen_edges = BTreeSet::new();
        let mut results = Vec::new();
        while let Some((name, depth)) = queue.pop_front() {
            if depth >= max_depth || !expanded.insert(name.clone()) {
                continue;
            }
            let references = match direction {
                TraceDirection::Callers => self.references_by_name.get(&name),
                TraceDirection::Callees => self.calls_by_caller.get(&name),
            };
            for reference_index in references.into_iter().flatten() {
                let reference = &self.references[*reference_index];
                if reference.kind != "call" || !in_scope(&reference.path, scope) {
                    continue;
                }
                let Some(owner) = &reference.caller else {
                    continue;
                };
                let (caller, callee, next) = match direction {
                    TraceDirection::Callers => (owner, &name, owner),
                    TraceDirection::Callees => (&name, &reference.name, &reference.name),
                };
                let edge_depth = depth + 1;
                if seen_edges.insert((
                    caller.clone(),
                    callee.clone(),
                    reference.path.clone(),
                    reference.line,
                )) {
                    results.push(DependencyEdge {
                        direction,
                        depth: edge_depth,
                        caller: caller.clone(),
                        callee: callee.clone(),
                        path: reference.path.clone(),
                        line: reference.line,
                        expression: reference.expression.clone(),
                        confidence: "low: unqualified syntax names only; bindings and receiver types are unresolved".into(),
                    });
                    if results.len() >= max_results {
                        return results;
                    }
                }
                if edge_depth < max_depth {
                    queue.push_back((next.clone(), edge_depth));
                }
            }
        }
        results
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
    fn inspect_symbol(&self, workspace: &Workspace, args: InspectArgs) -> Result<InspectResult> {
        validate_query(&args.name)?;
        let scope = scope(workspace, args.path.as_deref())?;
        // Hard caps by construction: orientation is worth a few hundred bytes, not a subgraph.
        const DEFINITIONS: usize = 3;
        const SIDE: usize = 6;
        const MEMBERS: usize = 8;
        let definitions: Vec<_> = self
            .symbols
            .get(&args.name)
            .into_iter()
            .flatten()
            .filter(|symbol| in_scope(&symbol.path, &scope))
            .collect();
        let mut rows: Vec<RelationRow> = definitions
            .iter()
            .take(DEFINITIONS)
            .map(|symbol| RelationRow {
                relation: "definition".into(),
                symbol: format!("{}::{}", symbol.path, symbol.name),
                kind: symbol.kind.clone(),
                path: symbol.path.clone(),
                line: symbol.line,
                container: symbol.container.clone(),
            })
            .collect();
        let mut side = |relation: &str, positions: Option<&Vec<usize>>, outbound: bool| {
            let mut seen = BTreeSet::new();
            let mut count = 0;
            for position in positions.into_iter().flatten() {
                let reference = &self.references[*position];
                if reference.kind != "call" || !in_scope(&reference.path, &scope) {
                    continue;
                }
                let Some(name) = (if outbound {
                    Some(reference.name.clone())
                } else {
                    reference.caller.clone()
                }) else {
                    continue;
                };
                if !seen.insert((reference.path.clone(), name.clone())) {
                    continue;
                }
                count += 1;
                if count <= SIDE {
                    rows.push(RelationRow {
                        relation: relation.into(),
                        symbol: format!("{}::{}", reference.path, name),
                        kind: if outbound { "call".into() } else { "caller".into() },
                        path: reference.path.clone(),
                        line: reference.line,
                        container: reference.caller.clone(),
                    });
                }
            }
            count
        };
        let inbound = side("inbound", self.references_by_name.get(&args.name), false);
        let outbound = side("outbound", self.calls_by_caller.get(&args.name), true);
        // Hierarchy: state the level explicitly so a class is never confused with its members.
        let members: Vec<_> = self
            .symbols
            .values()
            .flatten()
            .filter(|symbol| {
                symbol.container.as_deref() == Some(args.name.as_str())
                    && in_scope(&symbol.path, &scope)
            })
            .collect();
        for symbol in members.iter().take(MEMBERS) {
            rows.push(RelationRow {
                relation: "member".into(),
                symbol: format!("{}::{}", symbol.path, symbol.name),
                kind: symbol.kind.clone(),
                path: symbol.path.clone(),
                line: symbol.line,
                container: symbol.container.clone(),
            });
        }
        let (symbol_status, nearest_indexed_names) = self.seed_status(&args.name);
        let counts = BTreeMap::from([
            ("definitions".to_owned(), definitions.len()),
            ("inbound".to_owned(), inbound),
            ("outbound".to_owned(), outbound),
            ("members".to_owned(), members.len()),
        ]);
        let truncated = definitions.len() > DEFINITIONS
            || inbound > SIDE
            || outbound > SIDE
            || members.len() > MEMBERS;
        Ok(InspectResult {
            page: Page { results: rows, has_more: truncated, next_offset: None },
            counts,
            symbol_status,
            nearest_indexed_names,
            coverage: self.coverage.clone(),
            limitations: "One hop, capped per relation; counts are complete even when rows are \
                          truncated. Expand a side with find_callers or trace_dependencies. \
                          Candidates are syntax matches, not proven bindings.".into(),
        })
    }

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
        let (symbol_status, nearest_indexed_names) = self.seed_status(&args.name);
        Ok(StructuralResult {
            page: page(definitions, limit, offset),
            coverage: self.coverage.clone(),
            symbol_status,
            nearest_indexed_names,
        })
    }

    fn find_callers(&self, workspace: &Workspace, args: CallerArgs) -> Result<CallersResult> {
        validate_query(&args.name)?;
        let (limit, offset) = pagination(args.limit, args.offset)?;
        let scope = scope(workspace, args.path.as_deref())?;
        let candidates = self.symbols.get(&args.name).cloned().unwrap_or_default();
        let refs = self
            .references_by_name
            .get(&args.name)
            .into_iter()
            .flatten()
            .map(|index| &self.references[*index])
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
                    .filter(|symbol| same_language_family(&symbol.path, &reference.path))
                    .cloned()
                    .collect();
                CallerHit {
                    reference,
                    candidate_count: candidates.len(),
                    candidates_truncated: candidates.len() > 5,
                    resolution: match candidates.len() {
                        0 => "unresolved",
                        1 => "unique_name_candidate",
                        _ => "ambiguous",
                    }
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
            for target in candidates.iter().take(5) {
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
        let (symbol_status, nearest_indexed_names) = self.seed_status(&args.name);
        Ok(CallersResult {
            retrieval: StructuralResult {
                page: Page {
                    results,
                    has_more: refs.has_more,
                    next_offset: refs.next_offset,
                },
                coverage: self.coverage.clone(),
                symbol_status,
                nearest_indexed_names,
            },
            candidate_definitions: candidates.iter().take(5).cloned().collect(),
            candidate_definition_count: candidates.len(),
            candidate_definitions_truncated: candidates.len() > 5,
            confidence:
                "low: spelling match only; receiver type and lexical binding are unresolved".into(),
            imports,
            imports_truncated,
            orientation: self.orientation(&args.name, "callers", "inbound", &scope),
            file_relationships,
            relationships_truncated,
        })
    }

    fn trace_dependencies(
        &self,
        workspace: &Workspace,
        args: TraceArgs,
    ) -> Result<DependencyTraceResult> {
        validate_query(&args.name)?;
        let (limit, offset) = pagination(args.limit, args.offset)?;
        let max_depth = args.depth.unwrap_or(3);
        ensure!((1..=5).contains(&max_depth), "depth must be 1..5");
        let scope = scope(workspace, args.path.as_deref())?;
        let direction = args.direction.unwrap_or(TraceDirection::Callers);
        let mut roots: Vec<_> = self
            .symbols
            .get(&args.name)
            .into_iter()
            .flatten()
            .filter(|symbol| in_scope(&symbol.path, &scope))
            .take(6)
            .cloned()
            .collect();
        let roots_truncated = roots.len() > 5;
        roots.truncate(5);
        let edges = self.trace_edges(
            &args.name,
            direction,
            &scope,
            max_depth,
            offset + limit + 1,
        );
        let has_more = edges.len() > offset + limit;
        let (symbol_status, nearest_indexed_names) = self.seed_status(&args.name);
        let relation = match direction {
            TraceDirection::Callers => ("callers", "inbound"),
            TraceDirection::Callees => ("callees", "outbound"),
        };
        Ok(DependencyTraceResult {
            page: Page {
                results: edges.into_iter().skip(offset).take(limit).collect(),
                has_more,
                next_offset: has_more.then_some(offset + limit),
            },
            root_definitions: roots,
            roots_truncated,
            coverage: self.coverage.clone(),
            symbol_status,
            nearest_indexed_names,
            orientation: self.orientation(&args.name, relation.0, relation.1, &scope),
            limitations: "Candidate call paths only: unqualified syntax names can merge unrelated functions or methods. Verify material edges with read_source.".into(),
        })
    }

    fn search_concept(
        &self,
        workspace: &Workspace,
        query: &str,
        path: Option<&str>,
        wanted: usize,
    ) -> Result<Vec<RankedRegion>> {
        validate_query(query)?;
        ensure!((1..=100).contains(&wanted), "wanted must be 1..100");
        let scope = scope(workspace, path)?;
        Ok(self.concepts.search(query, &scope, wanted))
    }
    fn locate(&self, path: &str, line: usize) -> Option<SymbolLocation> {
        // The innermost enclosing definition: a method inside an impl inside a module wins.
        let symbol = self
            .symbols
            .values()
            .flatten()
            .filter(|symbol| symbol.path == path && (symbol.line..=symbol.end_line).contains(&line))
            .min_by_key(|symbol| symbol.end_line - symbol.line)?;
        let same_language =
            |reference: &Reference| same_language_family(&reference.path, &symbol.path);
        let name_candidate_callers =
            self.references_by_name
                .get(&symbol.name)
                .map_or(0, |positions| {
                    positions
                        .iter()
                        .filter(|position| {
                            let reference = &self.references[**position];
                            reference.kind == "call" && same_language(reference)
                        })
                        .count()
                });
        let direct_callees = self.calls_by_caller.get(&symbol.name).map_or(0, |positions| {
            positions
                .iter()
                .filter(|position| {
                    let reference = &self.references[**position];
                    reference.kind == "call"
                        && reference.path == symbol.path
                        && (symbol.line..=symbol.end_line).contains(&reference.line)
                })
                .count()
        });
        Some(SymbolLocation {
            symbol: format!("{}::{}", symbol.path, symbol.name),
            name: symbol.name.clone(),
            kind: symbol.kind.clone(),
            path: symbol.path.clone(),
            line: symbol.line,
            end_line: symbol.end_line,
            name_candidate_callers,
            direct_callees,
        })
    }

    fn coverage(&self) -> &Coverage {
        &self.coverage
    }
}

impl StructuralIndex {
    /// Indexed names sharing tokens or a substring with an unrecognised request, best first.
    ///
    /// A token carried by a large share of names - "the", "test", "a" - identifies nothing, so
    /// it neither qualifies a candidate nor adds to its score; rare shared tokens score by
    /// rarity. Frequencies are recomputed per call: this only runs on unknown-symbol requests,
    /// where one linear pass costs less than taxing every index build with a precomputed table.
    fn nearest_names(&self, name: &str) -> Vec<String> {
        let wanted: BTreeSet<_> = concept_tokens(name).into_iter().collect();
        let lowered = name.to_lowercase();
        let total = self.symbols.len().max(1);
        let mut frequency: BTreeMap<String, usize> = BTreeMap::new();
        for candidate in self.symbols.keys() {
            for token in BTreeSet::from_iter(concept_tokens(candidate)) {
                *frequency.entry(token).or_default() += 1;
            }
        }
        // Discriminating: near-unique, or carried by at most one name in twenty.
        let discriminating =
            |token: &str| frequency.get(token).is_none_or(|df| *df <= 2 || df * 20 <= total);
        let mut scored: Vec<_> = self
            .symbols
            .keys()
            .filter_map(|candidate| {
                let tokens: BTreeSet<_> = concept_tokens(candidate).into_iter().collect();
                let shared: usize = wanted
                    .intersection(&tokens)
                    .filter(|token| discriminating(token))
                    .map(|token| total / frequency.get(token.as_str()).copied().unwrap_or(1))
                    .sum();
                let lowered_candidate = candidate.to_lowercase();
                let contained =
                    lowered_candidate.contains(&lowered) || lowered.contains(&lowered_candidate);
                (shared > 0 || contained)
                    .then(|| (shared + if contained { total } else { 0 }, candidate.clone()))
            })
            .collect();
        scored.sort_by(|left, right| right.0.cmp(&left.0).then(left.1.cmp(&right.1)));
        scored.into_iter().take(5).map(|(_, name)| name).collect()
    }

    fn call_degrees(&self, name: &str, scope: &str) -> (usize, usize) {
        let calls = |index: Option<&Vec<usize>>| {
            index.map_or(0, |positions| {
                positions
                    .iter()
                    .filter(|position| {
                        let reference = &self.references[**position];
                        reference.kind == "call" && in_scope(&reference.path, scope)
                    })
                    .count()
            })
        };
        (
            calls(self.references_by_name.get(name)),
            calls(self.calls_by_caller.get(name)),
        )
    }

    /// Graph facts about which way an answer looked, never a recommendation of what to call next.
    fn orientation(&self, name: &str, relation: &str, direction: &str, scope: &str) -> Orientation {
        let (incoming_callers, outgoing_callees) = self.call_degrees(name, scope);
        let note = match direction {
            "inbound" if outgoing_callees > 0 => Some(format!(
                "this symbol also calls {outgoing_callees} indexed definitions"
            )),
            "outbound" if incoming_callers > 0 => Some(format!(
                "this symbol is also called from {incoming_callers} indexed sites"
            )),
            _ => None,
        };
        Orientation {
            symbol: name.to_owned(),
            incoming_callers,
            outgoing_callees,
            returned_relation: relation.to_owned(),
            direction: direction.to_owned(),
            note,
        }
    }

    fn seed_status(&self, name: &str) -> (String, Vec<String>) {
        if self.symbols.contains_key(name) {
            ("indexed".into(), Vec::new())
        } else {
            ("unknown_symbol".into(), self.nearest_names(name))
        }
    }
}


/// Read and parse one batch of files across `workers` threads, returning results in the order the
/// batch names them. Ordering is the whole point: the merge that follows writes symbol and
/// reference indices, so it has to see files in path order however the threads finished.
type Parsed = (String, Option<(String, Result<ParsedFile>)>);

fn parse_batch(
    workspace: &Workspace,
    batch: &[String],
    remaining: Duration,
    workers: usize,
) -> Vec<Parsed> {
    let one = |file: &String| -> Parsed {
        let Ok(source) = workspace.text(file) else {
            return (file.clone(), None);
        };
        let parsed = parse_file(file, &source, remaining);
        (file.clone(), Some((source, parsed)))
    };
    if workers <= 1 || batch.len() <= 1 {
        return batch.iter().map(one).collect();
    }
    let stride = batch.len().div_ceil(workers);
    let mut slices = Vec::new();
    std::thread::scope(|scope| {
        let handles: Vec<_> = batch
            .chunks(stride)
            .map(|slice| scope.spawn(move || slice.iter().map(&one).collect::<Vec<_>>()))
            .collect();
        for handle in handles {
            // A panicking parser would poison one slice; treat its files as unreadable rather
            // than losing the whole snapshot, and let the skip count say so.
            slices.push(handle.join().unwrap_or_default());
        }
    });
    slices.into_iter().flatten().collect()
}
struct ParsedFile {
    symbols: Vec<Symbol>,
    references: Vec<Reference>,
    imports: Vec<Import>,
    has_error: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum LanguageFamily {
    Rust,
    Python,
    EcmaScript,
    Go,
    Java,
    CFamily,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SourceLanguage {
    Rust,
    Python,
    TypeScript,
    Tsx,
    Go,
    Java,
    C,
    Cpp,
}

impl SourceLanguage {
    fn family(self) -> LanguageFamily {
        match self {
            Self::Rust => LanguageFamily::Rust,
            Self::Python => LanguageFamily::Python,
            Self::TypeScript | Self::Tsx => LanguageFamily::EcmaScript,
            Self::Go => LanguageFamily::Go,
            Self::Java => LanguageFamily::Java,
            Self::C | Self::Cpp => LanguageFamily::CFamily,
        }
    }

    fn grammar(self) -> tree_sitter::Language {
        match self {
            Self::Rust => tree_sitter_rust::LANGUAGE.into(),
            Self::Python => tree_sitter_python::LANGUAGE.into(),
            Self::TypeScript => tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
            Self::Tsx => tree_sitter_typescript::LANGUAGE_TSX.into(),
            Self::Go => tree_sitter_go::LANGUAGE.into(),
            Self::Java => tree_sitter_java::LANGUAGE.into(),
            Self::C => tree_sitter_c::LANGUAGE.into(),
            Self::Cpp => tree_sitter_cpp::LANGUAGE.into(),
        }
    }
}

fn source_language(path: &str) -> Option<SourceLanguage> {
    match Path::new(path).extension().and_then(|extension| extension.to_str()) {
        Some("rs") => Some(SourceLanguage::Rust),
        Some("py") => Some(SourceLanguage::Python),
        Some("js" | "mjs" | "cjs" | "ts" | "mts" | "cts") => Some(SourceLanguage::TypeScript),
        Some("jsx" | "tsx") => Some(SourceLanguage::Tsx),
        Some("go") => Some(SourceLanguage::Go),
        Some("java") => Some(SourceLanguage::Java),
        Some("c") => Some(SourceLanguage::C),
        Some("h" | "cc" | "cpp" | "cxx" | "c++" | "hh" | "hpp" | "hxx" | "h++" | "ipp"
        | "tpp") => Some(SourceLanguage::Cpp),
        _ => None,
    }
}

fn same_language_family(left: &str, right: &str) -> bool {
    source_language(left).map(SourceLanguage::family)
        == source_language(right).map(SourceLanguage::family)
}

fn node_text<'a>(node: Node<'_>, source: &'a str) -> &'a str {
    &source[node.byte_range()]
}
fn is_definition(language: SourceLanguage, kind: &str) -> bool {
    match language.family() {
        LanguageFamily::Rust => matches!(
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
        ),
        LanguageFamily::Python => matches!(kind, "function_definition" | "class_definition"),
        LanguageFamily::EcmaScript => matches!(
            kind,
            "function_declaration"
                | "generator_function_declaration"
                | "method_definition"
                | "class_declaration"
                | "abstract_class_declaration"
                | "interface_declaration"
                | "type_alias_declaration"
                | "enum_declaration"
                | "variable_declarator"
        ),
        LanguageFamily::Go => matches!(
            kind,
            "function_declaration"
                | "method_declaration"
                | "method_elem"
                | "type_spec"
                | "type_alias"
        ),
        LanguageFamily::Java => matches!(
            kind,
            "method_declaration"
                | "constructor_declaration"
                | "compact_constructor_declaration"
                | "class_declaration"
                | "interface_declaration"
                | "enum_declaration"
                | "record_declaration"
                | "annotation_type_declaration"
                | "annotation_type_element_declaration"
        ),
        LanguageFamily::CFamily => matches!(
            kind,
            "function_definition"
                | "struct_specifier"
                | "class_specifier"
                | "union_specifier"
                | "enum_specifier"
                | "type_definition"
                | "namespace_definition"
                | "alias_declaration"
                | "concept_definition"
                // A function-like macro is what a C caller actually invokes; `#define MAX(a, b)`
                // has no other definition anywhere, so without it every `MAX(...)` call site
                // resolves to nothing. Object-like macros stay out, as plain constants do.
                | "preproc_function_def"
        ),
    }
}

/// `const x = 5` is not a definition worth indexing; `const run = () => {}` is. Only an
/// ECMAScript-family declarator bound to a function shape counts, so ordinary local bindings do
/// not flood the index or become fictional caller owners.
fn holds_definition(language: SourceLanguage, node: Node<'_>) -> bool {
    if language.family() != LanguageFamily::EcmaScript || node.kind() != "variable_declarator" {
        return true;
    }
    node.child_by_field_name("value").is_some_and(|value| {
        matches!(
            value.kind(),
            "arrow_function" | "function_expression" | "function" | "class"
        )
    })
}

fn terminal_name(mut node: Node<'_>) -> Option<Node<'_>> {
    for _ in 0..32 {
        if matches!(
            node.kind(),
            "identifier"
                | "field_identifier"
                | "type_identifier"
                | "property_identifier"
                | "namespace_identifier"
                | "destructor_name"
                | "operator_name"
        ) {
            return Some(node);
        }
        // Several C and C++ declarators hold the thing they decorate as an ordinary child rather
        // than under a field: `typedef int (*Callback)(int)` parenthesises it, and
        // `const BlockHandle& metaindex_handle() const {` wraps it in a reference declarator. A
        // field-only descent stops at those and loses every function-pointer typedef and every
        // reference-returning accessor in a C++ corpus.
        let mut cursor = node.walk();
        node = node
            .child_by_field_name("name")
            .or_else(|| node.child_by_field_name("declarator"))
            .or_else(|| node.child_by_field_name("field"))
            .or_else(|| node.child_by_field_name("attribute"))
            .or_else(|| node.child_by_field_name("property"))
            .or_else(|| node.child_by_field_name("function"))
            .or_else(|| node.child_by_field_name("type"))
            .or_else(|| {
                matches!(
                    node.kind(),
                    "parenthesized_declarator"
                        | "reference_declarator"
                        | "attributed_declarator"
                        | "structured_binding_declarator"
                )
                .then(|| node.named_children(&mut cursor).next())
                .flatten()
            })?;
    }
    None
}

fn definition_name(language: SourceLanguage, node: Node<'_>) -> Option<Node<'_>> {
    node.child_by_field_name("name")
        .or_else(|| {
            matches!(language.family(), LanguageFamily::CFamily)
                .then(|| node.child_by_field_name("declarator"))
                .flatten()
        })
        .and_then(terminal_name)
}

fn first_descendant<'tree>(node: Node<'tree>, kinds: &[&str]) -> Option<Node<'tree>> {
    if kinds.contains(&node.kind()) {
        return Some(node);
    }
    let mut cursor = node.walk();
    node.named_children(&mut cursor)
        .find_map(|child| first_descendant(child, kinds))
}

fn enclosing_definition(
    language: SourceLanguage,
    mut node: Node<'_>,
    source: &str,
) -> Option<String> {
    while let Some(parent) = node.parent() {
        // A call inside `const value = helper()` belongs to the function holding the binding, not
        // to `value`. Only function-valued declarators pass the same definition predicate used by
        // the symbol index.
        if (is_definition(language, parent.kind())
            || (language == SourceLanguage::Rust && parent.kind() == "impl_item"))
            && holds_definition(language, parent)
        {
            let name = definition_name(language, parent)
                .or_else(|| parent.child_by_field_name("type").and_then(terminal_name));
            if let Some(name) = name {
                return Some(excerpt(node_text(name, source), 200));
            }
        }
        node = parent;
    }
    None
}

fn definition_container(
    language: SourceLanguage,
    node: Node<'_>,
    source: &str,
) -> Option<String> {
    if language == SourceLanguage::Go
        && node.kind() == "method_declaration"
        && let Some(receiver) = node.child_by_field_name("receiver")
        && let Some(receiver_type) = first_descendant(receiver, &["type_identifier"])
    {
        return Some(excerpt(node_text(receiver_type, source), 200));
    }
    if language == SourceLanguage::Cpp
        && node.kind() == "function_definition"
        && let Some(declarator) = node.child_by_field_name("declarator")
        && let Some(qualified) = first_descendant(declarator, &["qualified_identifier"])
        && let Some((container, _)) = node_text(qualified, source).rsplit_once("::")
    {
        return Some(excerpt(container, 200));
    }
    enclosing_definition(language, node, source)
}

fn is_import(language: SourceLanguage, kind: &str) -> bool {
    match language.family() {
        LanguageFamily::Rust => kind == "use_declaration",
        LanguageFamily::Python => matches!(kind, "import_statement" | "import_from_statement"),
        LanguageFamily::EcmaScript => kind == "import_statement",
        LanguageFamily::Go | LanguageFamily::Java => kind == "import_declaration",
        // `#include` is the C-family import, and only its quoted form names a file in this
        // repository; the angle-bracket form names a toolchain path that no corpus-relative
        // candidate can honestly claim.
        LanguageFamily::CFamily => kind == "preproc_include",
    }
}

/// The simple type a `new` expression constructs: the head of a generic type, the tail of a
/// scoped one.
fn constructed_name(mut node: Node<'_>) -> Option<Node<'_>> {
    for _ in 0..32 {
        let mut cursor = node.walk();
        node = match node.kind() {
            "type_identifier" => return Some(node),
            "generic_type" => node.named_children(&mut cursor).next()?,
            "scoped_type_identifier" => node.named_children(&mut cursor).last()?,
            _ => return terminal_name(node),
        };
    }
    None
}

/// The identifier a call expression is calling. Each grammar stores the final name under
/// different fields; descending through another call node is forbidden because `factory()()`
/// contains one direct call to `factory`, not two.
fn call_name(node: Node<'_>) -> Option<Node<'_>> {
    if matches!(node.kind(), "call_expression" | "call") {
        None
    } else {
        terminal_name(node)
    }
}

fn call_nodes(language: SourceLanguage, node: Node<'_>) -> Option<(Node<'_>, Node<'_>)> {
    if language == SourceLanguage::Java {
        return match node.kind() {
            "method_invocation" => {
                let name = node.child_by_field_name("name")?;
                Some((name, name))
            }
            // `new java.util.ArrayList<String>()` is a call to `ArrayList`. A generic type keeps
            // the constructed type first and its arguments after it, and a scoped type keeps the
            // simple name last, so neither can be reached by taking a fixed child.
            "object_creation_expression" => {
                let expression = node.child_by_field_name("type")?;
                Some((constructed_name(expression)?, expression))
            }
            _ => None,
        };
    }
    // `new Engine(8)`, `new Table(rows)`: a constructor call is a call site, and the grammars
    // keep it under `new_expression` rather than under a call expression. Without this, asking
    // who calls a class returns every factory that mentions it and none of the code that
    // actually constructs it - the same shape of silence as the TypeScript member calls that
    // once resolved to nothing.
    if matches!(language.family(), LanguageFamily::CFamily | LanguageFamily::EcmaScript)
        && node.kind() == "new_expression"
        && let Some(expression) = node
            .child_by_field_name("type")
            .or_else(|| node.child_by_field_name("constructor"))
    {
        return Some((terminal_name(expression)?, expression));
    }
    if !matches!(node.kind(), "call_expression" | "call") {
        return None;
    }
    let expression = node.child_by_field_name("function")?;
    Some((call_name(expression)?, expression))
}

fn parse_file(path: &str, source: &str, timeout: Duration) -> Result<ParsedFile> {
    let source_language =
        source_language(path).with_context(|| format!("unsupported source language: {path}"))?;
    let language = source_language.grammar();
    let mut parser = Parser::new();
    parser.set_language(&language)?;
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
    let file = FileText { language: source_language, path, source, lines: &lines };
    let mut definitions = BTreeSet::new();
    let mut call_names = BTreeSet::new();
    let mut spans = BTreeSet::new();
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
        if is_definition(source_language, node.kind())
            && holds_definition(source_language, *node)
            && let Some(name) = definition_name(source_language, *node)
        {
            definitions.insert(name.id());
            let line = node.start_position().row + 1;
            let end_line = node.end_position().row + 1;
            let text = excerpt(node_text(name, source), 200);
            // `typedef struct Table { ... } Table;` is one definition written twice: the typedef
            // and the struct it names occupy the same span under the same name, and reporting
            // both makes a unique C type read as an ambiguous namesake.
            if spans.insert((text.clone(), line, end_line)) {
                parsed.symbols.push(Symbol {
                    id: format!("{path}:{line}:{}", name.start_position().column + 1),
                    name: text,
                    kind: node.kind().into(),
                    path: path.into(),
                    line,
                    end_line,
                    container: definition_container(source_language, *node, source),
                    excerpt: excerpt(lines.get(line - 1).unwrap_or(&""), 300),
                });
            }
        }
        if let Some((name, expression)) = call_nodes(source_language, *node) {
            call_names.insert(name.id());
            parsed.references.push(reference(
                &file,
                *node,
                name,
                "call",
                node_text(expression, source),
            ));
        }
        if is_import(source_language, node.kind())
            || (source_language == SourceLanguage::Rust
                && node.kind() == "mod_item"
                && node.child_by_field_name("body").is_none())
        {
            parsed.imports.push(import(&file, *node));
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
            if is_import(source_language, ancestor.kind())
                || matches!(
                    ancestor.kind(),
                    "parameters"
                        | "parameter"
                        | "type_parameters"
                        | "parameter_list"
                        | "formal_parameters"
                )
            {
                excluded = true;
                break;
            }
            if is_definition(source_language, ancestor.kind()) {
                break;
            }
            parent = ancestor.parent();
        }
        if !excluded {
            parsed.references.push(reference(
                &file,
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

/// The file being parsed, so the row builders take a context rather than five loose arguments.
struct FileText<'a> {
    language: SourceLanguage,
    path: &'a str,
    source: &'a str,
    lines: &'a [&'a str],
}

fn reference(
    file: &FileText<'_>,
    node: Node<'_>,
    name: Node<'_>,
    kind: &str,
    expression: &str,
) -> Reference {
    let line = node.start_position().row + 1;
    Reference {
        name: excerpt(node_text(name, file.source), 200),
        kind: kind.into(),
        path: file.path.into(),
        line,
        column: node.start_position().column + 1,
        caller: enclosing_definition(file.language, node, file.source),
        expression: excerpt(expression, 200),
        excerpt: excerpt(file.lines.get(line - 1).unwrap_or(&""), 300),
    }
}

fn lexical_relative(parent: &Path, relative: &str) -> Option<std::path::PathBuf> {
    let mut resolved = parent.to_path_buf();
    for component in Path::new(relative).components() {
        match component {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                resolved.pop().then_some(())?;
            }
            std::path::Component::Normal(part) => resolved.push(part),
            _ => return None,
        }
    }
    Some(resolved)
}

fn ecmascript_import_candidates(parent: &Path, specifier: &str, candidates: &mut Vec<String>) {
    if !specifier.starts_with('.') {
        return;
    }
    let Some(base) = lexical_relative(parent, specifier) else {
        return;
    };
    let mut add = |path: std::path::PathBuf| {
        candidates.push(path.to_string_lossy().replace('\\', "/"));
    };
    match base.extension().and_then(|extension| extension.to_str()) {
        Some("js") => {
            add(base.clone());
            add(base.with_extension("ts"));
            add(base.with_extension("tsx"));
        }
        Some("jsx") => {
            add(base.clone());
            add(base.with_extension("tsx"));
        }
        Some("mjs") => {
            add(base.clone());
            add(base.with_extension("mts"));
        }
        Some("cjs") => {
            add(base.clone());
            add(base.with_extension("cts"));
        }
        Some(_) => add(base),
        None => {
            for extension in ["js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts"] {
                add(base.with_extension(extension));
                add(base.join("index").with_extension(extension));
            }
        }
    }
}

fn import(file: &FileText<'_>, node: Node<'_>) -> Import {
    let (language, path, source) = (file.language, file.path, file.source);
    let mut candidates = Vec::new();
    let parent = Path::new(path).parent().unwrap_or(Path::new(""));
    if language == SourceLanguage::Rust
        && node.kind() == "mod_item"
        && let Some(name) = node.child_by_field_name("name")
    {
        let name = node_text(name, source);
        // Rust's sibling module convention only; #[path], inline modules, and crate layout remain unresolved.
        let base = if matches!(
            Path::new(path).file_name().and_then(|file| file.to_str()),
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
    } else if language == SourceLanguage::Python
        && node.kind() == "import_from_statement"
        && let Some(module) = node.child_by_field_name("module_name")
    {
        let name = node_text(module, source);
        if !name.starts_with('.') {
            let name = name.replace('.', "/");
            candidates.push(format!("{name}.py"));
            candidates.push(format!("{name}/__init__.py"));
        }
    } else if language == SourceLanguage::Python && node.kind() == "import_statement" {
        let mut cursor = node.walk();
        for child in node.named_children(&mut cursor) {
            let child = child.child_by_field_name("name").unwrap_or(child);
            let name = node_text(child, source).replace('.', "/");
            candidates.push(format!("{name}.py"));
            candidates.push(format!("{name}/__init__.py"));
        }
    } else if language.family() == LanguageFamily::EcmaScript
        && let Some(specifier) = node.child_by_field_name("source")
    {
        ecmascript_import_candidates(
            parent,
            node_text(specifier, source).trim_matches(['\'', '"']),
            &mut candidates,
        );
    } else if language == SourceLanguage::Java {
        // `import com.example.Tool;` names a path relative to a source root the syntax does not
        // state. The bare package path and the conventional Maven/Gradle root are offered as
        // candidates; both are dropped later unless the file is really there.
        let mut cursor = node.walk();
        for child in node.named_children(&mut cursor) {
            let name = node_text(child, source).replace('.', "/");
            if name.contains('/') {
                candidates.push(format!("{name}.java"));
                candidates.push(format!("src/main/java/{name}.java"));
            }
        }
    } else if language.family() == LanguageFamily::CFamily
        && let Some(included) = node.child_by_field_name("path")
        && included.kind() == "string_literal"
    {
        let name = node_text(included, source).trim_matches('"');
        candidates.push(name.replace('\\', "/"));
        if let Some(relative) = lexical_relative(parent, name) {
            candidates.push(relative.to_string_lossy().replace('\\', "/"));
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

    /// A budget-truncated snapshot must be distinguishable from a complete one. `complete` is
    /// always false, so without this flag a partial index and a full index read identically, and
    /// an empty caller set reads as proof of absence when whole files were never scanned.
    #[test]
    fn a_truncated_snapshot_says_so_and_a_complete_one_does_not() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("a.rs"), "fn target() {}\n").unwrap();
        std::fs::write(dir.path().join("b.rs"), "fn caller() { target(); }\n").unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let files = vec!["a.rs".to_string(), "b.rs".to_string()];

        let full =
            StructuralIndex::from_files(&ws, files.clone(), Duration::from_secs(5)).unwrap();
        assert!(!full.coverage.budget_truncated);
        assert_eq!(full.coverage.indexed_files, 2);
        assert_eq!(full.coverage.eligible_files, 2);
        assert!(!full.coverage.limitations.contains("not evidence of absence"));

        // A zero build budget truncates before the first file, which is the same code path a
        // record or byte ceiling takes on a large repository.
        let starved = StructuralIndex::from_files(&ws, files, Duration::ZERO).unwrap();
        assert!(starved.coverage.budget_truncated);
        assert_eq!(starved.coverage.indexed_files, 0);
        assert_eq!(starved.coverage.eligible_files, 2, "eligibility is counted before the budget");
        assert!(starved.coverage.limitations.contains("not evidence of absence"));
        assert!(starved.coverage.limitations.contains("0 of 2 eligible files"));
    }

    /// The file walk honors ignore rules by default and `--no-ignore` reopens them, so the
    /// index and search_exact keep describing the same corpus in both modes.
    #[tokio::test]
    async fn build_honors_ignore_rules_unless_told_otherwise() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join(".ignore"), "skip.rs\n").unwrap();
        std::fs::write(dir.path().join("kept.rs"), "fn kept() {}\n").unwrap();
        std::fs::write(dir.path().join("skip.rs"), "fn skipped() {}\n").unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let honoring = StructuralIndex::build(ws.clone(), Duration::from_secs(5), false)
            .await
            .unwrap();
        assert_eq!(honoring.coverage.indexed_files, 1);
        let reopened = StructuralIndex::build(ws, Duration::from_secs(5), true)
            .await
            .unwrap();
        assert_eq!(reopened.coverage.indexed_files, 2);
    }
    /// "frobnicate_the_widget" shares only "the" with most sentence-style test names; that must
    /// not qualify them. The one name sharing the rare token "widget" is the only suggestion.
    #[test]
    fn nearest_names_ignore_ubiquitous_tokens() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("a.rs"),
            "fn check_the_widget_state() {}\nfn resolve_the_range_to_the_innermost_definition() {}\nfn parse_the_file_list() {}\nfn build_the_index_snapshot() {}\n",
        )
        .unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let index =
            StructuralIndex::from_files(&ws, vec!["a.rs".into()], Duration::from_secs(5)).unwrap();
        assert_eq!(
            index.nearest_names("frobnicate_the_widget"),
            vec!["check_the_widget_state".to_string()]
        );
        assert!(index.nearest_names("about_the_thing").is_empty());
    }

    /// The vocabulary of a question usually lives in the doc comment, not the body; the chunk
    /// must include it while the reported region stays the definition.
    #[test]
    fn concept_chunks_include_documentation_above_the_definition() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("fuse.rs"),
            "/// Reciprocal rank fusion of a dense and a lexical ranking.\nfn fuse_rankings() { let k = 60.0; }\nfn unrelated() { let x = 1; }\n",
        )
        .unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let index = StructuralIndex::from_files(&ws, vec!["fuse.rs".into()], Duration::from_secs(5))
            .unwrap();
        let hits = index
            .search_concept(&ws, "reciprocal rank fusion", None, 3)
            .unwrap();
        assert_eq!(hits[0].path, "fuse.rs");
        assert_eq!(hits[0].start_line, 2, "region is the definition, not the comment");
    }

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
    fn transitive_traces_respect_depth_direction_cycles_and_paging() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("chain.rs"),
            "fn leaf() {}\nfn middle() { leaf(); }\nfn top() { middle(); }\nfn spin() { spin_helper(); }\nfn spin_helper() { spin(); }\n",
        )
        .unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let index =
            StructuralIndex::from_files(&ws, vec!["chain.rs".into()], Duration::from_secs(5))
                .unwrap();
        let trace = |name: &str, direction, depth, limit, offset| {
            index
                .trace_dependencies(
                    &ws,
                    TraceArgs {
                        name: name.into(),
                        direction: Some(direction),
                        depth: Some(depth),
                        path: None,
                        limit,
                        offset,
                    },
                )
                .unwrap()
        };
        let one_hop = trace("leaf", TraceDirection::Callers, 1, None, None);
        assert_eq!(
            one_hop
                .page
                .results
                .iter()
                .map(|e| (e.caller.as_str(), e.callee.as_str(), e.depth))
                .collect::<Vec<_>>(),
            vec![("middle", "leaf", 1)]
        );
        assert_eq!(one_hop.root_definitions[0].line, 1);
        // Depth 2 reaches the indirect caller a direct-caller lookup cannot see.
        let two_hops = trace("leaf", TraceDirection::Callers, 2, None, None);
        assert_eq!(
            two_hops
                .page
                .results
                .iter()
                .map(|e| (e.caller.as_str(), e.callee.as_str(), e.depth))
                .collect::<Vec<_>>(),
            vec![("middle", "leaf", 1), ("top", "middle", 2)]
        );
        let callees = trace("top", TraceDirection::Callees, 3, None, None);
        assert_eq!(
            callees
                .page
                .results
                .iter()
                .map(|e| (e.caller.as_str(), e.callee.as_str(), e.depth))
                .collect::<Vec<_>>(),
            vec![("top", "middle", 1), ("middle", "leaf", 2)]
        );
        // A call cycle must terminate instead of revisiting expanded names.
        let cyclic = trace("spin", TraceDirection::Callees, 5, None, None);
        assert_eq!(cyclic.page.results.len(), 2);
        assert!(!cyclic.page.has_more);
        let first_page = trace("leaf", TraceDirection::Callers, 2, Some(1), None);
        assert!(first_page.page.has_more);
        assert_eq!(first_page.page.next_offset, Some(1));
        let second_page = trace("leaf", TraceDirection::Callers, 2, Some(1), Some(1));
        assert_eq!(second_page.page.results[0].caller, "top");
        assert!(!second_page.page.has_more);
        assert!(
            index
                .trace_dependencies(
                    &ws,
                    TraceArgs {
                        name: "leaf".into(),
                        direction: None,
                        depth: Some(6),
                        path: None,
                        limit: None,
                        offset: None,
                    },
                )
                .is_err()
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
    #[test]
    fn bm25_finds_definitions_from_a_description_without_a_model() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("text.rs"),
            "/// Wrap a paragraph so no output line exceeds the width.\nfn wrapParagraph(width: usize) -> usize { width }\nfn checksum_bytes(data: &[u8]) -> u64 { data.len() as u64 }\n",
        )
        .unwrap();
        std::fs::write(
            dir.path().join("other.rs"),
            "fn unrelated_helper() -> bool { true }\n",
        )
        .unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let index = StructuralIndex::from_files(
            &ws,
            vec!["text.rs".into(), "other.rs".into()],
            Duration::from_secs(5),
        )
        .unwrap();
        // A description that shares no exact identifier with the target still reaches it, because
        // camelCase is split and the doc comment is part of the document.
        let hits = index
            .search_concept(&ws, "wrap lines to a maximum width", None, 5)
            .unwrap();
        assert_eq!(hits[0].path, "text.rs");
        assert_eq!(hits[0].start_line, 2, "{hits:?}");
        assert!(hits[0].score > 0.0);
        // Scope restricts candidates; an unrelated file ranks nothing for this query.
        let scoped = index
            .search_concept(&ws, "wrap lines to a maximum width", Some("other.rs"), 5)
            .unwrap();
        assert!(scoped.is_empty(), "{scoped:?}");
        let checksum = index.search_concept(&ws, "checksum of bytes", None, 1).unwrap();
        assert_eq!(checksum[0].start_line, 3);
        assert!(index.search_concept(&ws, "  ", None, 5).is_err());
    }

    /// A method called on an object is a call site. TypeScript keeps the callee under a
    /// `property` field as a `property_identifier`, which the callee resolver did not know, so
    /// every `this.method()` and `obj.method()` was dropped: on VS Code's editor core, `getEdits`
    /// had fourteen real call sites and `find_callers` reported none, while free functions in the
    /// same corpus looked perfectly healthy.
    #[test]
    fn typescript_member_calls_are_call_sites() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("ops.ts"),
            "export class Operation {\n\
             \tgetEdits(value: number) { return value; }\n\
             \trun(value: number) { return this.getEdits(value); }\n\
             }\n\
             export function drive(operation: Operation) { return operation.getEdits(1); }\n\
             export function plain(value: number) { return value; }\n\
             export function callPlain() { return plain(2); }\n",
        )
        .unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let index =
            StructuralIndex::from_files(&ws, vec!["ops.ts".into()], Duration::from_secs(5)).unwrap();
        let callers = |name: &str| {
            let mut found: Vec<_> = index
                .references
                .iter()
                .filter(|reference| reference.name == name && reference.kind == "call")
                .filter_map(|reference| reference.caller.clone())
                .collect();
            found.sort();
            found
        };
        assert_eq!(
            callers("getEdits"),
            vec!["drive".to_string(), "run".to_string()]
        );
        assert_eq!(callers("plain"), vec!["callPlain".to_string()]);
    }

    /// `import_string(name)()` contains an outer invocation of the returned callable and one
    /// direct call to `import_string`. The outer call used to descend through the inner call and
    /// emit a second row at the same source position.
    #[test]
    fn immediately_invoked_return_value_is_not_a_second_call_to_the_factory() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("loading.py"),
            "def import_string(name):\n    return lambda: name\n\ndef load(name):\n    return import_string(name)()\n",
        )
        .unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let index = StructuralIndex::from_files(
            &ws,
            vec!["loading.py".into()],
            Duration::from_secs(5),
        )
        .unwrap();
        let calls: Vec<_> = index
            .references
            .iter()
            .filter(|reference| reference.name == "import_string" && reference.kind == "call")
            .collect();
        assert_eq!(calls.len(), 1, "{calls:?}");
        assert_eq!(calls[0].caller.as_deref(), Some("load"));
        assert_eq!(calls[0].expression, "import_string");
    }

    /// Candidate definitions and graph degrees have different scopes. Definition context remains
    /// repository-wide, but the path argument restricts both returned call sites and orientation
    /// counts. A bounded definition list must state exactly what it omitted.
    #[test]
    fn caller_context_exposes_definition_truncation_and_scopes_orientation() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("inside")).unwrap();
        std::fs::write(
            dir.path().join("inside/target.rs"),
            "fn helper_inside() {}\nfn target() { helper_inside(); }\nfn call_inside() { target(); }\n",
        )
        .unwrap();
        let mut files = vec!["inside/target.rs".to_string()];
        for number in 0..6 {
            let path = format!("outside{number}.rs");
            std::fs::write(
                dir.path().join(&path),
                format!(
                    "fn helper_{number}() {{}}\nfn target() {{ helper_{number}(); }}\nfn call_outside_{number}() {{ target(); }}\n"
                ),
            )
            .unwrap();
            files.push(path);
        }
        let ws = Workspace::new(dir.path()).unwrap();
        let index = StructuralIndex::from_files(&ws, files, Duration::from_secs(5)).unwrap();
        let found = index
            .find_callers(
                &ws,
                CallerArgs {
                    name: "target".into(),
                    path: Some("inside".into()),
                    include_references: None,
                    limit: Some(20),
                    offset: None,
                },
            )
            .unwrap();
        assert_eq!(found.retrieval.page.results.len(), 1);
        assert_eq!(found.candidate_definition_count, 7);
        assert_eq!(found.candidate_definitions.len(), 5);
        assert!(found.candidate_definitions_truncated);
        assert_eq!(found.orientation.incoming_callers, 1);
        assert_eq!(found.orientation.outgoing_callees, 1);
    }

    /// A call written into a local binding belongs to the function holding the binding. Reported
    /// as the binding, an exhaustive caller answer names a const that no caller could verify: on
    /// VS Code 1.96 every caller row for `getEnterAction` named `enterAction`, `r` or
    /// `expectedEnterAction` instead of the three methods the corpus actually defines.
    #[test]
    fn a_call_bound_to_a_local_const_is_attributed_to_its_enclosing_function() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("indent.ts"),
            "export function getEnterAction(line: number) { return line; }\n\
             export class ShiftCommand {\n\
             \tgetEditOperations(line: number) {\n\
             \t\tconst enterAction = getEnterAction(line);\n\
             \t\treturn enterAction;\n\
             \t}\n\
             }\n\
             export const run = (line: number) => getEnterAction(line);\n",
        )
        .unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let index =
            StructuralIndex::from_files(&ws, vec!["indent.ts".into()], Duration::from_secs(5))
                .unwrap();
        let callers: Vec<_> = index
            .references
            .iter()
            .filter(|reference| reference.name == "getEnterAction" && reference.kind == "call")
            .filter_map(|reference| reference.caller.clone())
            .collect();
        assert_eq!(
            callers,
            vec!["getEditOperations".to_string(), "run".to_string()],
            "a local const is not a caller; an arrow function bound to a const is"
        );
    }
    #[test]
    fn typescript_definitions_calls_and_generic_names_are_indexed() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("service.ts"),
            "const RETRIES = 3;\n\
             /** Trim each incoming record to the configured column budget. */\n\
             export const handler = (rows: string[]) => rows.map((row) => row.trim());\n\
             export class Manager {\n  process(data: string[]) { return handler(data); }\n}\n\
             export interface Options { width: number }\n\
             export function service(options: Options) { return new Manager(); }\n",
        )
        .unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let index =
            StructuralIndex::from_files(&ws, vec!["service.ts".into()], Duration::from_secs(5))
                .unwrap();
        let names: BTreeSet<_> = index.symbols.keys().cloned().collect();
        assert!(names.contains("handler"), "{names:?}");
        assert!(names.contains("Manager") && names.contains("process"), "{names:?}");
        assert!(names.contains("Options") && names.contains("service"), "{names:?}");
        // A plain constant is not a definition, so TypeScript does not flood the index.
        assert!(!names.contains("RETRIES"), "{names:?}");
        let callers = index
            .find_callers(
                &ws,
                CallerArgs {
                    name: "handler".into(),
                    path: None,
                    include_references: None,
                    limit: None,
                    offset: None,
                },
            )
            .unwrap();
        assert_eq!(
            callers.retrieval.page.results[0].reference.caller.as_deref(),
            Some("process")
        );
        // Generic identifiers are exactly where lexical ranking is supposed to struggle; the doc
        // comment is what carries the description into the BM25 document.
        let hits = index
            .search_concept(&ws, "trim incoming records to a column budget", None, 3)
            .unwrap();
        assert_eq!(index.locate(&hits[0].path, hits[0].start_line).unwrap().name, "handler");
    }
    #[test]
    fn neighbourhood_shows_both_sides_and_counts_beyond_its_caps() {
        let dir = tempfile::tempdir().unwrap();
        // Eight callers exceeds the six-row cap; the count must still report all of them.
        let calls: String = (1..=8)
            .map(|n| format!("class C{n} {{ go{n}() {{ target(); }} }}\n"))
            .collect();
        std::fs::write(
            dir.path().join("app.ts"),
            format!(
                "{calls}export function target() {{ helper(); other(); }}\n\
                 function helper() {{}}\nfunction other() {{}}\n"
            ),
        )
        .unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let index =
            StructuralIndex::from_files(&ws, vec!["app.ts".into()], Duration::from_secs(5)).unwrap();
        let out = index
            .inspect_symbol(&ws, InspectArgs { name: "target".into(), path: None })
            .unwrap();
        let relation = |kind: &str| relation_names(&out, kind);
        assert_eq!(relation("definition"), ["target"]);
        // Both directions arrive in one call: that is the point of the primitive.
        assert_eq!(relation("inbound").len(), 6);
        assert_eq!(out.counts["inbound"], 8);
        assert!(out.page.has_more);
        let mut outbound = relation("outbound");
        outbound.sort();
        assert_eq!(outbound, ["helper", "other"]);
        assert_eq!(out.symbol_status, "indexed");
        // Orientation is worth a few hundred bytes; a subgraph is not.
        assert!(serde_json::to_string(&out.page).unwrap().len() < 2048);
        // The level is explicit: a container answers with its members, not with itself.
        let class = index
            .inspect_symbol(&ws, InspectArgs { name: "C1".into(), path: None })
            .unwrap();
        assert_eq!(relation_names(&class, "member"), ["go1"]);
        let unknown = index
            .inspect_symbol(&ws, InspectArgs { name: "targe".into(), path: None })
            .unwrap();
        assert_eq!(unknown.symbol_status, "unknown_symbol");
        assert!(unknown.nearest_indexed_names.contains(&"target".to_string()));
    }

    fn relation_names(out: &InspectResult, kind: &str) -> Vec<String> {
        out.page
            .results
            .iter()
            .filter(|row| row.relation == kind)
            .map(|row| row.symbol.rsplit("::").next().unwrap().to_owned())
            .collect()
    }

    #[test]
    fn identifier_tokens_split_on_case_and_underscore() {
        // Non-alphanumerics split first, then camelCase, and the whole word is kept as well, so a
        // query word matches wrapParagraph, wrap_paragraph, and the literal identifier alike.
        assert_eq!(
            concept_tokens("wrapParagraph_width"),
            vec!["wrap", "paragraph", "wrapparagraph", "width"]
        );
        assert_eq!(concept_tokens("HTTPServer"), vec!["httpserver"]);
        assert!(concept_tokens("   ").is_empty());
    }
    #[test]
    fn ranges_resolve_to_the_innermost_definition_with_honest_degrees() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("chain.rs"),
            "fn leaf() {}\nstruct Thing;\nimpl Thing {\n    fn run(&self) {\n        leaf();\n        leaf();\n    }\n}\nfn top() { leaf(); }\n",
        )
        .unwrap();
        std::fs::write(
            dir.path().join("empty.rs"),
            "struct Empty;\nimpl Empty {\n    fn dispose(&self) {}\n}\nfn call(empty: Empty) { empty.dispose(); }\n",
        )
        .unwrap();
        std::fs::write(
            dir.path().join("active.rs"),
            "fn cleanup() {}\nstruct Active;\nimpl Active {\n    fn dispose(&self) { cleanup(); }\n}\n",
        )
        .unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let index = StructuralIndex::from_files(
            &ws,
            vec!["active.rs".into(), "chain.rs".into(), "empty.rs".into()],
            Duration::from_secs(5),
        )
        .unwrap();
        // Line 5 sits inside run, which sits inside the impl block: the tighter span wins.
        let inner = index.locate("chain.rs", 5).unwrap();
        assert_eq!(inner.symbol, "chain.rs::run");
        assert_eq!(inner.direct_callees, 2);
        assert_eq!(inner.name_candidate_callers, 0);
        let leaf = index.locate("chain.rs", 1).unwrap();
        assert_eq!(leaf.name, "leaf");
        assert_eq!(leaf.name_candidate_callers, 3);
        // Same-named definitions share candidate callers, but their owned body counts are exact.
        let empty = index.locate("empty.rs", 3).unwrap();
        let active = index.locate("active.rs", 4).unwrap();
        assert_eq!(empty.name_candidate_callers, 1);
        assert_eq!(active.name_candidate_callers, 1);
        assert_eq!(empty.direct_callees, 0);
        assert_eq!(active.direct_callees, 1);
        assert!(index.locate("missing.rs", 1).is_none());
        let spans =
            StructuralIndex::symbol_spans("chain.rs", "fn only() {}\n", Duration::from_secs(2))
                .unwrap();
        assert_eq!(spans.len(), 1);
        assert_eq!(spans[0].name, "only");
    }
    #[test]
    fn unknown_seeds_fail_loudly_and_answers_state_their_direction() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("shell.ts"),
            "export function getResolvedShellEnv() { return doResolveUnixShellEnv(); }\nfunction doResolveUnixShellEnv() { return 1; }\nexport function consumer() { return getResolvedShellEnv(); }\n",
        )
        .unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let index =
            StructuralIndex::from_files(&ws, vec!["shell.ts".into()], Duration::from_secs(5))
                .unwrap();
        let invented = index
            .find_callers(
                &ws,
                CallerArgs {
                    name: "getShellEnvironment".into(),
                    path: None,
                    include_references: None,
                    limit: None,
                    offset: None,
                },
            )
            .unwrap();
        // A hallucinated identifier must not read as a proven absence.
        assert_eq!(invented.retrieval.symbol_status, "unknown_symbol");
        assert!(
            invented
                .retrieval
                .nearest_indexed_names
                .contains(&"getResolvedShellEnv".to_string()),
            "{:?}",
            invented.retrieval.nearest_indexed_names
        );
        let known = index
            .find_callers(
                &ws,
                CallerArgs {
                    name: "getResolvedShellEnv".into(),
                    path: None,
                    include_references: None,
                    limit: None,
                    offset: None,
                },
            )
            .unwrap();
        assert_eq!(known.retrieval.symbol_status, "indexed");
        assert!(known.retrieval.nearest_indexed_names.is_empty());
        // Asking inbound states that an outbound side exists, without prescribing a next call.
        assert_eq!(known.orientation.returned_relation, "callers");
        assert_eq!(known.orientation.direction, "inbound");
        assert_eq!(known.orientation.outgoing_callees, 1);
        assert_eq!(known.orientation.incoming_callers, 1);
        assert!(
            known.orientation.note.as_deref().unwrap().contains("also calls 1"),
            "{:?}",
            known.orientation.note
        );
        let outbound = index
            .trace_dependencies(
                &ws,
                TraceArgs {
                    name: "getResolvedShellEnv".into(),
                    direction: Some(TraceDirection::Callees),
                    depth: Some(2),
                    path: None,
                    limit: None,
                    offset: None,
                },
            )
            .unwrap();
        assert_eq!(outbound.orientation.direction, "outbound");
        assert!(
            outbound.orientation.note.as_deref().unwrap().contains("also called from"),
            "{:?}",
            outbound.orientation.note
        );
    }

    /// Build a snapshot over a written corpus, so a language test reads as the source it is about.
    fn snapshot(files: &[(&str, &str)]) -> (tempfile::TempDir, Workspace, StructuralIndex) {
        let dir = tempfile::tempdir().unwrap();
        let mut paths = Vec::new();
        for (path, source) in files {
            let full = dir.path().join(path);
            std::fs::create_dir_all(full.parent().unwrap()).unwrap();
            std::fs::write(full, source).unwrap();
            paths.push((*path).to_string());
        }
        paths.sort();
        let workspace = Workspace::new(dir.path()).unwrap();
        let index =
            StructuralIndex::from_files(&workspace, paths, Duration::from_secs(5)).unwrap();
        (dir, workspace, index)
    }

    fn caller_rows(index: &StructuralIndex, workspace: &Workspace, name: &str) -> Vec<String> {
        let found = index
            .find_callers(
                workspace,
                CallerArgs {
                    name: name.into(),
                    path: None,
                    include_references: None,
                    limit: None,
                    offset: None,
                },
            )
            .unwrap();
        let mut rows: Vec<_> = found
            .retrieval
            .page
            .results
            .iter()
            .map(|hit| {
                format!(
                    "{}::{}:{}",
                    hit.reference.path,
                    hit.reference.caller.clone().unwrap_or_default(),
                    hit.resolution
                )
            })
            .collect();
        rows.sort();
        rows
    }

    /// JavaScript and TypeScript are one language, written in eight extensions. Matching a call
    /// to a candidate definition by file extension would make a `.js` caller of a `.ts` helper
    /// unresolvable, and a specifier written `./util.js` - which every ESM build emits for a
    /// TypeScript source - would resolve to nothing.
    #[test]
    fn javascript_and_typescript_resolve_as_one_language_family() {
        let (_dir, workspace, index) = snapshot(&[
            (
                "src/util.ts",
                "export function formatRow(row: string): string { return row.trim(); }\n",
            ),
            (
                "src/app.js",
                "import { formatRow } from './util.js';\n\
                 export class Table {\n\
                 \trender(rows) { return rows.map((row) => formatRow(row)); }\n\
                 }\n\
                 export const build = (rows) => new Table(rows);\n",
            ),
            (
                "src/view.jsx",
                "export default function view(rows) { return rows.map(formatRow); }\n",
            ),
            (
                "other/format.rs",
                "fn formatRow() {}\nfn rust_caller() { formatRow(); }\n",
            ),
        ]);
        // A .js call site resolves against the .ts definition; the Rust namesake is a different
        // language and must not be offered as its candidate.
        assert_eq!(
            caller_rows(&index, &workspace, "formatRow"),
            vec![
                "other/format.rs::rust_caller:unique_name_candidate".to_string(),
                "src/app.js::render:unique_name_candidate".to_string(),
            ]
        );
        let names: BTreeSet<_> = index.symbols.keys().cloned().collect();
        assert!(names.contains("view"), "a .jsx module is indexed: {names:?}");
        let specifier = index
            .imports
            .iter()
            .find(|import| import.path == "src/app.js")
            .unwrap();
        assert_eq!(specifier.candidate_files, vec!["src/util.ts".to_string()]);
        assert_eq!(specifier.resolution, "possible_local_module");
        // `new Table(rows)` constructs the class, and a caller question about a class is almost
        // always about exactly that. Reading only call expressions left every constructor
        // invocation in a JavaScript or TypeScript corpus invisible.
        assert_eq!(
            caller_rows(&index, &workspace, "Table"),
            vec!["src/app.js::build:unique_name_candidate".to_string()]
        );
    }

    /// A Go method belongs to its receiver type, not to the file: `func (t *Table) Format(...)`
    /// is `Table::Format`, and an interface method belongs to the interface. Without the
    /// receiver, every method in a package reads as a free function and namesakes across types
    /// cannot be told apart.
    #[test]
    fn go_methods_are_owned_by_their_receiver_and_interface() {
        let (_dir, workspace, index) = snapshot(&[(
            "service.go",
            "package service\n\n\
             import \"strings\"\n\n\
             type Formatter interface {\n\
             \tFormat(row string) string\n\
             }\n\n\
             type Table struct{ Rows []string }\n\n\
             func (t *Table) Format(row string) string { return strings.TrimSpace(row) }\n\n\
             func (t Table) Render() []string {\n\
             \tout := []string{}\n\
             \tfor _, row := range t.Rows {\n\
             \t\tout = append(out, t.Format(row))\n\
             \t}\n\
             \treturn out\n\
             }\n\n\
             func NewTable(rows []string) *Table { return &Table{Rows: rows} }\n\n\
             func Report(rows []string) []string {\n\
             \ttable := NewTable(rows)\n\
             \tgo func() { _ = table.Format(\"x\") }()\n\
             \treturn table.Render()\n\
             }\n",
        )]);
        let containers: Vec<_> = index
            .symbols
            .get("Format")
            .unwrap()
            .iter()
            .map(|symbol| symbol.container.clone().unwrap_or_default())
            .collect();
        assert_eq!(containers, vec!["Formatter".to_string(), "Table".to_string()]);
        // A call inside a goroutine literal belongs to the function that launched it.
        assert_eq!(
            caller_rows(&index, &workspace, "Format"),
            vec![
                "service.go::Render:ambiguous".to_string(),
                "service.go::Report:ambiguous".to_string(),
            ]
        );
        assert_eq!(
            caller_rows(&index, &workspace, "NewTable"),
            vec!["service.go::Report:unique_name_candidate".to_string()]
        );
        // A package path is not a repository file, so no candidate is invented for it.
        let import = index.imports.first().unwrap();
        assert!(import.candidate_files.is_empty(), "{import:?}");
        assert_eq!(import.resolution, "unresolved");
    }

    /// Java invokes through `method_invocation` and constructs through `object_creation_expression`,
    /// neither of which is a `call_expression`. Reading only call expressions would report zero
    /// call sites for an entire Java corpus, and a constructed type written
    /// `new java.util.ArrayList<>()` names `ArrayList`, not `java`.
    #[test]
    fn java_invocations_and_constructions_are_call_sites() {
        let (_dir, workspace, index) = snapshot(&[
            (
                "com/example/Table.java",
                "package com.example;\n\n\
                 import java.util.ArrayList;\n\n\
                 public class Table {\n\
                 \tpublic Table(java.util.List<String> rows) { this.rows = rows; }\n\
                 \tpublic String format(String row) { return row.trim(); }\n\
                 \tpublic java.util.List<String> render() {\n\
                 \t\tjava.util.List<String> out = new java.util.ArrayList<>();\n\
                 \t\tout.add(format(\"row\"));\n\
                 \t\treturn out;\n\
                 \t}\n\
                 \tprivate java.util.List<String> rows;\n\
                 }\n",
            ),
            (
                "com/example/Report.java",
                "package com.example;\n\n\
                 import com.example.Table;\n\n\
                 public record Report(String title) {\n\
                 \tpublic Report {\n\
                 \t\ttitle = title.trim();\n\
                 \t}\n\
                 \tpublic java.util.List<String> lines() {\n\
                 \t\treturn new Table(new java.util.ArrayList<>()).render();\n\
                 \t}\n\
                 }\n",
            ),
        ]);
        assert_eq!(
            caller_rows(&index, &workspace, "render"),
            vec!["com/example/Report.java::lines:unique_name_candidate".to_string()]
        );
        assert_eq!(
            caller_rows(&index, &workspace, "format"),
            vec!["com/example/Table.java::render:unique_name_candidate".to_string()]
        );
        assert_eq!(
            caller_rows(&index, &workspace, "ArrayList"),
            vec![
                "com/example/Report.java::lines:unresolved".to_string(),
                "com/example/Table.java::render:unresolved".to_string(),
            ]
        );
        // A record, its compact constructor and a class constructor are definitions; the
        // constructor is owned by the type it builds.
        let constructors: Vec<_> = index
            .symbols
            .get("Report")
            .unwrap()
            .iter()
            .map(|symbol| (symbol.kind.as_str(), symbol.container.as_deref()))
            .collect();
        assert_eq!(
            constructors,
            vec![
                ("record_declaration", None),
                ("compact_constructor_declaration", Some("Report")),
            ]
        );
        // `import com.example.Table;` names a file this corpus really holds.
        let import = index
            .imports
            .iter()
            .find(|import| import.statement.contains("com.example.Table"))
            .unwrap();
        assert_eq!(
            import.candidate_files,
            vec!["com/example/Table.java".to_string()]
        );
    }

    /// C names a function through a declarator rather than a `name` field, wraps a
    /// function-pointer typedef in parentheses, and calls function-like macros that have no
    /// other definition. A typedef'd struct is also one definition written twice, and reporting
    /// both halves makes a unique type read as an ambiguous namesake.
    #[test]
    fn c_definitions_cover_declarators_macros_and_typedefs_once() {
        let (_dir, workspace, index) = snapshot(&[
            (
                "src/table.h",
                "typedef struct Table {\n\tint size;\n} Table;\n\
                 typedef int (*Callback)(int);\n\
                 int table_size(const Table *table);\n",
            ),
            (
                "src/table.c",
                "#include \"table.h\"\n\
                 #include <stdio.h>\n\n\
                 #define DOUBLE(value) ((value) * 2)\n\n\
                 static int normalize(int value) { return value < 0 ? 0 : value; }\n\n\
                 int table_size(const Table *table) { return normalize(table->size); }\n\n\
                 char *table_label(const Table *table) {\n\
                 \tprintf(\"%d\", DOUBLE(table_size(table)));\n\
                 \treturn 0;\n\
                 }\n",
            ),
        ]);
        let table: Vec<_> = index
            .symbols
            .get("Table")
            .unwrap()
            .iter()
            .map(|symbol| (symbol.path.as_str(), symbol.kind.as_str()))
            .collect();
        assert_eq!(
            table,
            vec![("src/table.h", "type_definition")],
            "the typedef and the struct it names are one definition"
        );
        let names: BTreeSet<_> = index.symbols.keys().cloned().collect();
        assert!(names.contains("Callback"), "{names:?}");
        assert!(names.contains("table_label") && names.contains("normalize"), "{names:?}");
        // A macro call site resolves, because the macro itself is the definition.
        assert_eq!(
            caller_rows(&index, &workspace, "DOUBLE"),
            vec!["src/table.c::table_label:unique_name_candidate".to_string()]
        );
        assert_eq!(
            caller_rows(&index, &workspace, "normalize"),
            vec!["src/table.c::table_size:unique_name_candidate".to_string()]
        );
        // A quoted include names a file in this repository; an angle-bracket include names a
        // toolchain path no corpus-relative candidate can honestly claim.
        let quoted = index
            .imports
            .iter()
            .find(|import| import.statement.contains("table.h"))
            .unwrap();
        assert_eq!(quoted.candidate_files, vec!["src/table.h".to_string()]);
        let angled = index
            .imports
            .iter()
            .find(|import| import.statement.contains("stdio.h"))
            .unwrap();
        assert!(angled.candidate_files.is_empty(), "{angled:?}");
    }

    /// A C++ method defined out of line carries its class in the declarator, not in an enclosing
    /// node: `std::string Engine::render(...)` sits at file scope, so without reading the
    /// qualified name the row names a method with no owner at all. `new Engine(8)` is a call
    /// site under `new_expression`, which is not a call expression.
    #[test]
    fn cpp_out_of_line_methods_keep_their_class_and_new_is_a_call() {
        let (_dir, workspace, index) = snapshot(&[
            (
                "engine.hpp",
                "#pragma once\n\
                 namespace report {\n\
                 class Engine {\n\
                 public:\n\
                 \texplicit Engine(int width);\n\
                 \tint width() const { return width_; }\n\
                 \tconst Options &options() const { return options_; }\n\
                 \tint render(int row) const;\n\
                 private:\n\
                 \tint width_;\n\
                 };\n\
                 template <typename T> T clampWidth(T value) { return value; }\n\
                 }\n",
            ),
            (
                "engine.cpp",
                "#include \"engine.hpp\"\n\
                 namespace report {\n\
                 Engine::Engine(int width) : width_(clampWidth(width)) {}\n\n\
                 int Engine::render(int row) const { return row + width(); }\n\n\
                 Engine *make() { return new Engine(8); }\n\
                 }\n",
            ),
        ]);
        let render = index.symbols.get("render").unwrap();
        assert_eq!(render.len(), 1);
        assert_eq!(render[0].path, "engine.cpp");
        assert_eq!(render[0].container.as_deref(), Some("Engine"));
        // The inline method is owned by the class body that holds it.
        assert_eq!(
            index.symbols.get("width").unwrap()[0].container.as_deref(),
            Some("Engine")
        );
        // A reference-returning accessor wraps its declarator in a `reference_declarator`, which
        // holds the name as an ordinary child rather than under a field: LevelDB's
        // `const BlockHandle& metaindex_handle() const {` was indexed as no definition at all.
        assert_eq!(
            index.symbols.get("options").unwrap()[0].container.as_deref(),
            Some("Engine")
        );
        assert_eq!(
            caller_rows(&index, &workspace, "Engine"),
            vec!["engine.cpp::make:ambiguous".to_string()]
        );
        assert_eq!(
            caller_rows(&index, &workspace, "clampWidth"),
            vec!["engine.cpp::Engine:unique_name_candidate".to_string()]
        );
        assert_eq!(
            caller_rows(&index, &workspace, "width"),
            vec!["engine.cpp::render:unique_name_candidate".to_string()]
        );
        // A namespace can span a whole file; chunking it would re-index every member it holds,
        // so the concept ranker answers with the definition, never the namespace around it.
        let hits = index
            .search_concept(&workspace, "clamp a width value", None, 3)
            .unwrap();
        assert_eq!(index.locate(&hits[0].path, hits[0].start_line).unwrap().name, "clampWidth");
        assert!(
            index.symbols.contains_key("report"),
            "the namespace itself stays indexed as a definition"
        );
    }
}
