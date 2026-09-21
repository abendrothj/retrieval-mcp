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

#[derive(Clone, Debug, Deserialize, JsonSchema)]
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

#[derive(Clone, Debug, Deserialize, JsonSchema)]
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

#[derive(Clone, Debug, Deserialize, JsonSchema)]
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

/// One call site or identifier reference.
///
/// `kind` is omitted for a plain call and `expression` when it merely repeats `name`, because
/// both are the ordinary case and a repeated field is re-sent with every later request of the
/// session. Measured per-call context growth on Linux was 6,042 tokens here against 4,378 for a
/// shell agent whose output the client truncates on the way in.
#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct Reference {
    pub name: String,
    #[serde(skip_serializing_if = "is_plain_call")]
    pub kind: String,
    pub path: String,
    pub line: usize,
    pub column: usize,
    pub caller: Option<String>,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub expression: String,
    pub excerpt: String,
}

/// A reference whose kind is the default a caller page is made of.
fn is_plain_call(kind: &str) -> bool {
    kind == "call"
}

#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct Import {
    pub path: String,
    pub line: usize,
    pub statement: String,
    pub candidate_files: Vec<String>,
    pub resolution: String,
}

/// Ceilings on one snapshot build. They exist so a pathological repository cannot exhaust memory;
/// they are not a statement about what the tools can index. When one of them stops the build,
/// `Coverage::budget_truncated` says so rather than leaving a partial index indistinguishable
/// from a complete one.
///
/// Measured on Linux 6.12 (60,283 eligible files, 1.32 GB of C), an M4 Pro, release build: the
/// record ceiling is the one that binds, and it binds first by a wide margin - 8,500 files, 52 MB
/// of source, 57 s and 1.4 GB resident, with nine minutes of a ten-minute timeout still unspent.
/// So `--timeout-seconds` is *not* the limit that normally binds on a large repository, and the
/// 1.4 GB figure belongs to the record ceiling rather than to the byte ceiling, which that corpus
/// never reached. Lifting all three indexes the whole kernel in 78-93 s and somewhere between 5.9
/// and 7.9 GB resident - that spread is one binary and one corpus measured four times, so treat
/// the number as "several gigabytes" and not as a figure. The ceilings are the difference between
/// a partial answer and that, not between a fast answer and a slow one.
pub struct Budget;

impl Budget {
    pub const FILES: usize = 20_000;
    pub const BYTES: usize = 128 * 1024 * 1024;
    pub const RECORDS: usize = 2_000_000;
}

/// What the index can read, and what it cannot promise. Both modes say the same thing here,
/// because both run the same parser over the same grammars.
const LANGUAGES: [&str; 6] =
    ["Rust", "Python", "JavaScript/TypeScript", "Go", "Java", "C/C++"];
const SYNTAX_LIMITATIONS: &str = "Syntax only: no type checking, macro expansion, dynamic \
    dispatch, alias/re-export or package resolution. Name matches are candidates, including when \
    unique. Hidden files and .git are excluded; ignore files are honored unless the server runs \
    with --no-ignore. Verify uncertain results with read_source.";

/// How many of a description's words seed a ranking, and how many files that seeding reads.
///
/// Every word, not the rare-looking ones. Seeding on a query's four longest words was measured
/// first, on the theory that length stands in for the corpus rarity no scan can know, and it lost
/// eight answers in 148: a question's long words are English - "immediately", "qualified",
/// "definition" - while the word that finds the file is short and technical - `wsgi`, `flush`,
/// `fd`, `tls`. Six of the nine lost golds sat in files carrying none of the four. Seeding on
/// every word of three characters or more scored exactly what ranking the whole corpus scores,
/// suite for suite. The cap is a guard against a pathological query, not a filter.
const CONCEPT_TOKENS: usize = 64;
const CONCEPT_FILES: usize = 400;

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

#[derive(Clone, Debug, Deserialize, JsonSchema)]
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
///
/// Every field here is paid for on every later request in the session, because a client re-sends
/// the whole conversation each time: measured per-call context growth on Linux was 6,042 tokens
/// for this server against 4,378 for a shell agent, whose output the client truncates before it
/// ever reaches the model. So the block says each thing once. `symbol` is `path::name` and the
/// separate `name` and `path` it used to repeat are recoverable from it; `line` and `end_line`
/// appear only when the definition's span differs from the row's own, which is the only case
/// where they carry information.
#[derive(Clone, Debug, Serialize, JsonSchema)]
pub struct SymbolLocation {
    pub symbol: String,
    pub kind: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub end_line: Option<usize>,
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

/// A page of ranked regions and the number of source files the ranking read to produce it.
pub struct Ranked {
    pub regions: Vec<RankedRegion>,
    pub files: usize,
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

/// One call site. Anything identical for every row of the page is stated once on the page, not
/// once per row: a client re-sends the whole conversation on every later request, so a repeated
/// field is paid for again and again. The same argument already moved `candidate_definitions` up
/// here and removed 27-61% of a caller response; these fields are the rest of it.
#[derive(Serialize, JsonSchema)]
pub struct CallerHit {
    #[serde(flatten)]
    pub reference: Reference,
    /// Present only where this row disagrees with the page's `resolution`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub candidate_count: Option<usize>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub candidates_truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resolution: Option<String>,
}

#[derive(Serialize, JsonSchema)]
pub struct CallersResult {
    #[serde(flatten)]
    pub retrieval: StructuralResult<CallerHit>,
    /// The definitions of the requested name. Identical for every row - they are the definitions
    /// of the name that was asked about - so they are stated once for the page rather than once per
    /// call site. Measured: 27-61% of a caller response was that repetition.
    pub candidate_definitions: Vec<Symbol>,
    /// How the rows resolve to those definitions, and how many candidates each row had, when
    /// every row agrees - which is the ordinary case, because they all concern one name. A row
    /// that disagrees carries its own `resolution` and `candidate_count`.
    pub resolution: Option<String>,
    pub candidate_count: Option<usize>,
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
    /// Rank definition-shaped regions for a natural-language description, without any model, and
    /// say how many source files that ranking read: a page of hits means one thing over a whole
    /// repository and another over the four hundred files a description pointed at.
    fn search_concept(
        &self,
        workspace: &Workspace,
        query: &str,
        path: Option<&str>,
        wanted: usize,
    ) -> Result<Ranked>;
    /// The innermost indexed definition containing the given line, if any.
    fn locate(&self, path: &str, line: usize) -> Option<SymbolLocation>;
    /// Which of `names` this definition's body calls.
    ///
    /// A ranking that reaches the right neighbourhood and returns two plausible siblings leaves
    /// the model to choose, and on the kernel suite it chose wrong four times - asking
    /// `find_callers` about `tls_alert_send` where the question described
    /// `tls_handshake_close`, the function whose last line calls it. The relationship between two
    /// returned rows is already in the index, so the page can state it instead of making the
    /// model spend a request inferring it.
    fn calls_among(&self, path: &str, line: usize, names: &[String]) -> Vec<String>;
    /// The snapshot's coverage, so callers can report corpus size alongside derived results.
    fn coverage(&self) -> &Coverage;
}

pub struct StructuralIndex {
    /// This module's import prefix, read from `go.mod`, when the corpus root is a Go module.
    /// Without it a qualified call cannot be resolved to a directory and none is filtered.
    module_path: Option<String>,
    /// Whether identifier references were kept. A snapshot drops them; a scan keeps them, because
    /// the one reader that wants them is answered from a scan.
    keeps_references: bool,
    symbols: BTreeMap<String, Vec<Symbol>>,
    references: Vec<Reference>,
    references_by_name: BTreeMap<String, Vec<usize>>,
    calls_by_caller: BTreeMap<String, Vec<usize>>,
    imports: Vec<Import>,
    concepts: Bm25,
    pub coverage: Coverage,
}

impl StructuralIndex {
    /// The corpus-relative directory a qualified Go call names, when it can be known exactly.
    ///
    /// `icredentials.ClientHandshakeInfoFromContext(...)` written inside `credentials.go` calls
    /// `internal/credentials`, not the definition on the line above it, and crediting the latter
    /// makes a row say a function calls itself. Aliases make the qualifier useless on its own -
    /// `otelinternaltracing` names `.../internal/tracing` - so the alias is resolved to its import
    /// path and the module path from `go.mod` turns that into a directory in this corpus. Without
    /// a module path nothing is claimed and nothing is filtered: a wrong drop costs a real caller,
    /// which is worse than the row it would remove.
    fn qualified_package_dir(&self, reference: &Reference) -> Option<String> {
        if !reference.path.ends_with(".go") {
            return None;
        }
        let module = self.module_path.as_deref()?;
        let (qualifier, _) = reference.expression.split_once('.')?;
        let qualifier = qualifier.trim();
        if qualifier.is_empty() || !qualifier.chars().all(|c| c.is_alphanumeric() || c == '_') {
            return None;
        }
        let target = self
            .imports
            .iter()
            .filter(|import| import.path == reference.path)
            .find_map(|import| import_target(&import.statement, qualifier))?;
        // An import outside this module cannot be satisfied by any file in the corpus, so the
        // empty directory it resolves to leaves the row `unresolved` - which is what it is.
        Some(match target.strip_prefix(module).map(|rest| rest.trim_start_matches('/')) {
            Some(relative) => relative.to_owned(),
            None => String::new(),
        })
    }

    /// Every row the parser found, identifier references included. This is what a candidate scan
    /// builds: the file set is small, and `find_callers(include_references: true)` has to be
    /// answerable from it.
    pub fn from_files(workspace: &Workspace, files: Vec<String>, timeout: Duration) -> Result<Self> {
        Self::build(workspace, files, timeout, true)
    }

    /// A whole-repository snapshot, which keeps call sites and drops every other reference.
    ///
    /// Measured 2026-09-19, references are about 90% of an index's records and only 18-25% of
    /// them are calls: Linux `mm` holds 206,526 references over 186 files, of which 41,673 are
    /// call sites. The rest exist for one optional flag, and every other reader - the trace, the
    /// neighbourhood, `locate`, the caller page itself - filters them out again. A snapshot
    /// therefore does not build them, and the flag that wants them is answered by a scan over the
    /// files that name the symbol.
    pub fn snapshot(workspace: &Workspace, files: Vec<String>, timeout: Duration) -> Result<Self> {
        Self::build(workspace, files, timeout, false)
    }

    fn build(
        workspace: &Workspace,
        files: Vec<String>,
        timeout: Duration,
        keep_references: bool,
    ) -> Result<Self> {
        let timestamp = now_ms();
        let mut index = Self {
            // One read at snapshot time: a qualified Go call can only be resolved to a directory
            // when the corpus root is the module those paths are written against.
            module_path: module_path(workspace),
            keeps_references: keep_references,
            symbols: BTreeMap::new(),
            references: Vec::new(),
            references_by_name: BTreeMap::new(),
            calls_by_caller: BTreeMap::new(),
            imports: Vec::new(),
            concepts: Bm25::default(),
            coverage: Coverage {
                snapshot_id: timestamp.to_string(),
                indexed_at_ms: timestamp,
                languages: LANGUAGES.iter().map(|name| (*name).to_owned()).collect(),
                indexed_files: 0,
                eligible_files: 0,
                unsupported_files: 0,
                skipped_files: 0,
                skipped_examples: Vec::new(),
                parse_error_files: 0,
                complete: false,
                budget_truncated: false,
                freshness: "Full snapshot built on first structural call; restart the server after edits to rebuild.".into(),
                limitations: SYNTAX_LIMITATIONS.into(),
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
                    // The ceiling counts what the index holds, not what the parser saw: a
                    // snapshot that drops identifier references must be allowed the files their
                    // absence pays for.
                    let kept = if keep_references {
                        parsed.references.len()
                    } else {
                        parsed.references.iter().filter(|row| row.kind == "call").count()
                    };
                    total_records += parsed.symbols.len() + kept + parsed.imports.len();
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
                        if !keep_references && reference.kind != "call" {
                            continue;
                        }
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

/// The import path an alias or package name refers to, from one Go import statement.
///
/// Handles both spellings: `"a/b/tracing"`, whose qualifier is its last segment, and
/// `otelinternaltracing "a/b/internal/tracing"`, whose qualifier is written in front of it.
/// This corpus's Go module path, from `go.mod` at its root.
///
/// A corpus that is a subdirectory of a module, or not Go at all, has none, and a call qualified
/// by a package alias is then left resolved by name alone - the conservative reading this server
/// has always given it.
fn module_path(workspace: &Workspace) -> Option<String> {
    let text = std::fs::read_to_string(workspace.root().join("go.mod")).ok()?;
    text.lines()
        .find_map(|line| line.trim().strip_prefix("module "))
        .map(|module| module.trim().to_owned())
}


fn import_target(statement: &str, qualifier: &str) -> Option<String> {
    for line in statement.lines() {
        let line = line.trim().trim_start_matches("import").trim();
        let Some(start) = line.find('"') else { continue };
        let rest = &line[start + 1..];
        let Some(end) = rest.find('"') else { continue };
        let target = &rest[..end];
        let alias = line[..start].trim().trim_start_matches('(').trim();
        let names_it = if alias.is_empty() {
            target.rsplit('/').next() == Some(qualifier)
        } else {
            alias == qualifier
        };
        if names_it {
            return Some(target.to_owned());
        }
    }
    None
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
        // A snapshot holds call sites only. Answering `include_references` from it would return a
        // smaller set than the question asked for and look like an answer, so the backend routes
        // that flag to a scan; this says so if anything ever wires it differently.
        ensure!(
            self.keeps_references || !args.include_references.unwrap_or(false),
            "this index holds call sites only; identifier references are resolved by scanning the \
             files that name the symbol"
        );
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
                let package_dir = self.qualified_package_dir(&reference);
                let candidates: Vec<_> = candidates
                    .iter()
                    .filter(|symbol| same_language_family(&symbol.path, &reference.path))
                    // `syscall.Fdatasync(...)` written inside this corpus's own `Fdatasync` calls
                    // the standard library, not the definition beside it, and reporting that
                    // definition as a candidate makes the row say a function calls itself. When
                    // the qualifier is a package this file imports, only a definition living in
                    // that package can be what the call reached; a qualifier that is a receiver
                    // or a local variable is left alone, because resolving those needs types.
                    .filter(|symbol| match &package_dir {
                        Some(directory) => Path::new(&symbol.path).parent()
                            == Some(Path::new(directory)),
                        None => true,
                    })
                    .cloned()
                    .collect();
                CallerHit {
                    reference,
                    candidate_count: Some(candidates.len()),
                    candidates_truncated: candidates.len() > 5,
                    resolution: Some(
                        match candidates.len() {
                            0 => "unresolved",
                            1 => "unique_name_candidate",
                            _ => "ambiguous",
                        }
                        .into(),
                    ),
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
        // Every row of a caller page concerns one name, so `resolution` and `candidate_count`
        // are almost always the same value repeated per row. State it once where it is uniform,
        // and leave it on the rows that disagree.
        let mut results = results;
        let uniform = results
            .first()
            .map(|first| (first.resolution.clone(), first.candidate_count))
            .filter(|(resolution, count)| {
                results.iter().all(|row| &row.resolution == resolution && row.candidate_count == *count)
            });
        if uniform.is_some() {
            for row in &mut results {
                row.resolution = None;
                row.candidate_count = None;
            }
        }
        let (page_resolution, page_candidate_count) =
            uniform.map_or((None, None), |(resolution, count)| (resolution, count));
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
            resolution: page_resolution,
            candidate_count: page_candidate_count,
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
    ) -> Result<Ranked> {
        validate_query(query)?;
        ensure!((1..=100).contains(&wanted), "wanted must be 1..100");
        let scope = scope(workspace, path)?;
        Ok(Ranked {
            regions: self.concepts.search(query, &scope, wanted),
            files: self.coverage.indexed_files,
        })
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
            kind: symbol.kind.clone(),
            line: Some(symbol.line),
            end_line: Some(symbol.end_line),
            name_candidate_callers,
            direct_callees,
        })
    }

    fn calls_among(&self, path: &str, line: usize, names: &[String]) -> Vec<String> {
        let Some(symbol) = self
            .symbols
            .values()
            .flatten()
            .filter(|symbol| symbol.path == path && symbol.line <= line && line <= symbol.end_line)
            .min_by_key(|symbol| symbol.end_line - symbol.line)
        else {
            return Vec::new();
        };
        let mut found: Vec<String> = names
            .iter()
            .filter(|name| name.as_str() != symbol.name)
            .filter(|name| {
                self.references_by_name.get(name.as_str()).is_some_and(|positions| {
                    positions.iter().any(|position| {
                        let reference = &self.references[*position];
                        reference.kind == "call"
                            && reference.path == symbol.path
                            && (symbol.line..=symbol.end_line).contains(&reference.line)
                    })
                })
            })
            .cloned()
            .collect();
        found.dedup();
        found
    }

    fn coverage(&self) -> &Coverage {
        &self.coverage
    }
}

/// One symbol name as a regex that matches it as a whole word. `\b` is only a word boundary next
/// to a word character, so a name that does not start or end with one - a C++ destructor written
/// `~Engine` - anchors only on the side that has one; every other character is escaped so a name
/// can never be read as a pattern.
fn scan_pattern(name: &str) -> String {
    let mut pattern = String::with_capacity(name.len() + 8);
    let word = |c: char| c.is_alphanumeric() || c == '_';
    if name.starts_with(word) {
        pattern.push_str(r"\b");
    }
    for character in name.chars() {
        if !word(character) {
            pattern.push('\\');
        }
        pattern.push(character);
    }
    if name.ends_with(word) {
        pattern.push_str(r"\b");
    }
    pattern
}

/// One query word as a regex that matches it as a word *of code*.
///
/// `scan_pattern` anchors on `\b`, which is right for a symbol scan and wrong for a description:
/// `_` is a word character, so `\bbucket\b` never matches `quiesce_bucket`, and snake_case is
/// how a C corpus spells exactly the thing a question describes. Here the boundary is any
/// character that is not a letter or digit, so `_`, `.`, `->` and end of line all separate words,
/// and a query saying "probe entry" can select the file that writes `kprobe_on_func_entry`.
fn concept_pattern(token: &str) -> String {
    let mut inner = String::with_capacity(token.len() + 8);
    for character in token.chars() {
        if !(character.is_alphanumeric() || character == '_') {
            inner.push('\\');
        }
        inner.push(character);
    }
    format!("(?:^|[^0-9A-Za-z])(?:{inner})")
}

/// Every source file whose text names one of `names`, found by the walk `search_exact` uses, plus
/// the number of source files that walk saw. Reading the corpus is what a caller question already
/// costs; this stops one file short of that, because a file that never writes the name cannot
/// call it.
fn files_naming(
    workspace: &Workspace,
    names: &[&str],
    timeout: Duration,
    no_ignore: bool,
) -> Result<(Vec<String>, usize)> {
    use grep_searcher::{BinaryDetection, SearcherBuilder, Sink, SinkMatch};

    struct Found<'a>(&'a mut bool);
    impl Sink for Found<'_> {
        type Error = std::io::Error;
        fn matched(
            &mut self,
            _searcher: &grep_searcher::Searcher,
            _matched: &SinkMatch<'_>,
        ) -> Result<bool, std::io::Error> {
            *self.0 = true;
            Ok(false)
        }
    }

    let alternatives = names
        .iter()
        .map(|name| scan_pattern(name))
        .collect::<Vec<_>>()
        .join("|");
    ensure!(!alternatives.is_empty(), "a candidate scan needs at least one name");
    let matcher = grep_regex::RegexMatcherBuilder::new()
        .line_terminator(Some(b'\n'))
        .build(&format!("(?:{alternatives})"))
        .context("cannot build the candidate scan pattern")?;
    let deadline = Instant::now() + timeout;
    let mut overrides = ignore::overrides::OverrideBuilder::new(workspace.root());
    overrides.add("!.git/**")?;
    overrides.add("!target/**")?;
    let mut walk = ignore::WalkBuilder::new(workspace.root());
    walk.overrides(overrides.build()?).hidden(true).max_filesize(Some(2 * 1024 * 1024));
    if no_ignore {
        walk.ignore(false)
            .git_ignore(false)
            .git_global(false)
            .git_exclude(false)
            .parents(false);
    }
    let mut searcher = SearcherBuilder::new()
        .binary_detection(BinaryDetection::quit(b'\x00'))
        .line_number(false)
        .build();
    let (mut candidates, mut eligible) = (Vec::new(), 0usize);
    for entry in walk.build() {
        ensure!(
            Instant::now() < deadline,
            "candidate scan timed out; narrow --root or increase --timeout-seconds"
        );
        let entry = entry.context("cannot walk the repository")?;
        if !entry.file_type().is_some_and(|kind| kind.is_file()) {
            continue;
        }
        let Ok(relative) = workspace.relative(entry.path()) else {
            continue;
        };
        if source_language(&relative).is_none() {
            continue;
        }
        eligible += 1;
        let mut found = false;
        if searcher.search_path(&matcher, entry.path(), Found(&mut found)).is_ok() && found {
            candidates.push(relative);
        }
    }
    Ok((candidates, eligible))
}

/// The source files a description most plausibly concerns, best first, with the number of source
/// files the walk saw.
///
/// Ranking definitions against a description needs the corpus's definitions, which is the one
/// thing a repository too large to index cannot give. A description is still made of words, and a
/// file that writes none of them holds no definition worth ranking, so the walk scores each file
/// by how many of the query's distinct tokens it writes and keeps the best `wanted`. Scoring is
/// what makes this usable: taking the first `wanted` files that match anything would take them in
/// path order, which on Linux means answering every question out of `arch/` - exactly the failure
/// a truncated snapshot already has.
fn files_about(
    workspace: &Workspace,
    tokens: &[String],
    timeout: Duration,
    no_ignore: bool,
    wanted: usize,
) -> Result<(Vec<String>, usize)> {
    use grep_searcher::{BinaryDetection, SearcherBuilder, Sink, SinkMatch};
    use std::sync::atomic::{AtomicBool, Ordering as AtomicOrdering};
    use std::sync::Mutex;

    /// Which of the query's tokens this file writes. A matched line is lowercased once and tested
    /// for every token, and the file stops being read as soon as it has shown all of them or
    /// spends its line budget.
    ///
    /// The budget decides what the score is allowed to see, and 32 lines was too few for a reason
    /// that only appears on a large tree: a long source file spends the whole budget on lines
    /// carrying the query's common words and never reaches the one line with the rare identifier,
    /// and a long source file is exactly where a kernel answer lives. The budget exists to bound a
    /// pathological file, not to sample a normal one.
    struct Seen<'a> {
        tokens: &'a [String],
        hit: &'a mut Vec<bool>,
        budget: usize,
    }
    impl Sink for Seen<'_> {
        type Error = std::io::Error;
        fn matched(
            &mut self,
            _searcher: &grep_searcher::Searcher,
            matched: &SinkMatch<'_>,
        ) -> Result<bool, std::io::Error> {
            self.budget -= 1;
            let Ok(text) = std::str::from_utf8(matched.bytes()) else {
                return Ok(self.budget > 0);
            };
            let lowered = text.to_lowercase();
            for (position, token) in self.tokens.iter().enumerate() {
                if !self.hit[position] && lowered.contains(token.as_str()) {
                    self.hit[position] = true;
                }
            }
            Ok(self.budget > 0 && self.hit.iter().any(|seen| !seen))
        }
    }
    const LINES_PER_FILE: usize = 2_048;

    ensure!(!tokens.is_empty(), "a concept scan needs at least one query token");
    let alternatives =
        tokens.iter().map(|token| concept_pattern(token)).collect::<Vec<_>>().join("|");
    let matcher = grep_regex::RegexMatcherBuilder::new()
        .case_insensitive(true)
        .line_terminator(Some(b'\n'))
        .build(&format!("(?:{alternatives})"))
        .context("cannot build the concept scan pattern")?;
    let deadline = Instant::now() + timeout;
    let mut overrides = ignore::overrides::OverrideBuilder::new(workspace.root());
    overrides.add("!.git/**")?;
    overrides.add("!target/**")?;
    let mut walk = ignore::WalkBuilder::new(workspace.root());
    walk.overrides(overrides.build()?)
        .hidden(true)
        .max_filesize(Some(2 * 1024 * 1024))
        .sort_by_file_path(Path::cmp);
    if no_ignore {
        walk.ignore(false)
            .git_ignore(false)
            .git_global(false)
            .git_exclude(false)
            .parents(false);
    }
    // Reading the tree one file after another is what made a 32-line budget look necessary. The
    // walk is I/O and regex, both of which scale across cores, and the result stays deterministic
    // because order is imposed below on scores and paths, never on arrival.
    //
    // Each worker keeps its own tallies and merges them once, when the walk drops it. Merging on a
    // threshold instead loses whatever the last partial batch held - on a three-file repository
    // that is every file, which is how the unit test caught it.
    type Carried = (Vec<(Vec<bool>, String)>, Vec<usize>, usize);
    struct Worker<'a> {
        workspace: &'a Workspace,
        tokens: &'a [String],
        matcher: &'a grep_regex::RegexMatcher,
        searcher: grep_searcher::Searcher,
        deadline: Instant,
        timed_out: &'a AtomicBool,
        shared: &'a Mutex<Carried>,
        local: Carried,
    }
    impl ignore::ParallelVisitor for Worker<'_> {
        fn visit(&mut self, entry: Result<ignore::DirEntry, ignore::Error>) -> ignore::WalkState {
            if Instant::now() >= self.deadline {
                self.timed_out.store(true, AtomicOrdering::Relaxed);
                return ignore::WalkState::Quit;
            }
            let Ok(entry) = entry else { return ignore::WalkState::Continue };
            if !entry.file_type().is_some_and(|kind| kind.is_file()) {
                return ignore::WalkState::Continue;
            }
            let Ok(relative) = self.workspace.relative(entry.path()) else {
                return ignore::WalkState::Continue;
            };
            if source_language(&relative).is_none() {
                return ignore::WalkState::Continue;
            }
            self.local.2 += 1;
            let mut hit = vec![false; self.tokens.len()];
            let sink = Seen { tokens: self.tokens, hit: &mut hit, budget: LINES_PER_FILE };
            if self.searcher.search_path(self.matcher, entry.path(), sink).is_err() {
                return ignore::WalkState::Continue;
            }
            if hit.iter().any(|seen| *seen) {
                for (position, seen) in hit.iter().enumerate() {
                    self.local.1[position] += usize::from(*seen);
                }
                self.local.0.push((hit, relative));
            }
            ignore::WalkState::Continue
        }
    }
    impl Drop for Worker<'_> {
        fn drop(&mut self) {
            let mut total = self.shared.lock().unwrap_or_else(|error| error.into_inner());
            total.0.append(&mut self.local.0);
            for (slot, count) in total.1.iter_mut().zip(self.local.1.iter_mut()) {
                *slot += std::mem::take(count);
            }
            total.2 += std::mem::take(&mut self.local.2);
        }
    }
    struct Workers<'a> {
        workspace: &'a Workspace,
        tokens: &'a [String],
        matcher: &'a grep_regex::RegexMatcher,
        deadline: Instant,
        timed_out: &'a AtomicBool,
        shared: &'a Mutex<Carried>,
    }
    impl<'a> ignore::ParallelVisitorBuilder<'a> for Workers<'a> {
        fn build(&mut self) -> Box<dyn ignore::ParallelVisitor + 'a> {
            Box::new(Worker {
                workspace: self.workspace,
                tokens: self.tokens,
                matcher: self.matcher,
                searcher: SearcherBuilder::new()
                    .binary_detection(BinaryDetection::quit(b'\x00'))
                    .line_number(false)
                    .build(),
                deadline: self.deadline,
                timed_out: self.timed_out,
                shared: self.shared,
                local: (Vec::new(), vec![0usize; self.tokens.len()], 0),
            })
        }
    }
    let shared: Mutex<Carried> = Mutex::new((Vec::new(), vec![0usize; tokens.len()], 0usize));
    let timed_out = AtomicBool::new(false);
    walk.build_parallel().visit(&mut Workers {
        workspace,
        tokens,
        matcher: &matcher,
        deadline,
        timed_out: &timed_out,
        shared: &shared,
    });
    let (carried, frequency, eligible) = {
        let mut total = shared.into_inner().unwrap_or_else(|error| error.into_inner());
        (std::mem::take(&mut total.0), std::mem::take(&mut total.1), total.2)
    };
    ensure!(
        !timed_out.load(AtomicOrdering::Relaxed),
        "concept scan timed out; narrow --root or increase --timeout-seconds"
    );
    // Rarity, accumulated by the walk that just finished: a token half the corpus writes separates
    // nothing, and one that five files write separates everything. `ln(1 + eligible/df)` is the
    // usual shape, and the +1 keeps a token every file carries at a small positive weight, so a
    // query made entirely of common words still ranks something.
    let weight: Vec<f64> = frequency
        .iter()
        .map(|&count| (1.0f64 + eligible as f64 / count.max(1) as f64).ln())
        .collect();
    let mut scored: Vec<(f64, String)> = carried
        .into_iter()
        .map(|(hit, path)| {
            let score: f64 = hit
                .iter()
                .enumerate()
                .filter(|(_, seen)| **seen)
                .map(|(position, _)| weight[position])
                .sum();
            (score, path)
        })
        .collect();
    // Rarest words first; path order decides ties, so the same question over the same bytes always
    // reads the same files.
    scored.sort_by(|left, right| {
        right
            .0
            .partial_cmp(&left.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| left.1.cmp(&right.1))
    });
    scored.truncate(wanted);
    let mut files: Vec<String> = scored.into_iter().map(|(_, path)| path).collect();
    files.sort();
    Ok((files, eligible))
}

/// The backend a session answers structural questions with. There is one, and it holds no index
/// between calls.
///
/// A whole-repository snapshot was the obvious design and it has two failures a search does not.
/// It cannot cover a large repository - on Linux it reaches 13,581 of 60,283 files and every
/// caller set is then partial by construction - and, worse for the only client this server has, it
/// answers from bytes that are no longer on disk. A session is a session in which code changes: a
/// snapshot built at the first call reports one caller for a helper that has two by the time the
/// agent writes the second, and the only cue is a freshness string. A scan reads the repository
/// when the question is asked, so it is right about a file saved a second ago.
///
/// Every question is addressed by something the corpus can be searched for: a name for the caller
/// and symbol tools, the description's own words for ranking, one path for `locate`. The parsing,
/// the attribution and the row construction are what they always were - a `StructuralIndex` is
/// built over the files the search found and asked the question - which is why the answers are
/// identical to the ones a snapshot gave, row for row across nine corpora and suite for suite
/// across seven question sets.
///
/// What this costs is a walk per call: tens of milliseconds on an ordinary repository, a second or
/// two on Linux. What it buys is that no answer is ever stale, `absence` means absence, and the
/// memory a session holds is bounded by its largest single question rather than by the repository.
pub struct Retrieval {
    workspace: Workspace,
    timeout: Duration,
    no_ignore: bool,
    /// What `coverage()` reports: there is no standing index, and a page of rows says for itself
    /// how much of the repository it read.
    standing: Coverage,
}

impl Retrieval {
    pub fn new(workspace: Workspace, timeout: Duration, no_ignore: bool) -> Self {
        Self {
            workspace,
            timeout,
            no_ignore,
            standing: Coverage {
                snapshot_id: "per-question".into(),
                indexed_at_ms: now_ms(),
                languages: LANGUAGES.iter().map(|name| (*name).to_owned()).collect(),
                indexed_files: 0,
                eligible_files: 0,
                unsupported_files: 0,
                skipped_files: 0,
                skipped_examples: Vec::new(),
                parse_error_files: 0,
                complete: false,
                budget_truncated: false,
                freshness: "No standing index: every question searches the repository as it is on \
                            disk and parses only the files that answer it, so an edit made a \
                            moment ago is already visible."
                    .into(),
                limitations: SYNTAX_LIMITATIONS.into(),
            },
        }
    }

    /// An index over exactly the files that name these symbols, reporting what it read.
    fn scan(&self, names: &[&str]) -> Result<StructuralIndex> {
        let (files, eligible) =
            files_naming(&self.workspace, names, self.timeout, self.no_ignore)?;
        let scanned = files.len();
        let mut index = StructuralIndex::from_files(&self.workspace, files, self.timeout)?;
        index.coverage.eligible_files = eligible;
        index.coverage.freshness = "Resolved for this question: the repository was searched for \
                                    the requested name and only the files that write it were \
                                    parsed. Nothing is cached between calls."
            .into();
        index.coverage.limitations = format!(
            "{} This answer parsed the {scanned} of {eligible} source files that name the \
             requested symbol, so it covers the whole repository for this symbol: a caller absent \
             here writes the name nowhere the walk can see.",
            index.coverage.limitations
        );
        Ok(index)
    }

    /// An index over the files a description's own words point at, ranked and capped.
    ///
    /// A description points at thousands of files in a large repository, and the `CONCEPT_FILES`
    /// that carry most of its words cost a few seconds of walking and half a second of parsing -
    /// against the minute a snapshot spends to cover a fifth of the tree and then answer out of
    /// whatever the walk reached first.
    fn about(&self, query: &str) -> Result<StructuralIndex> {
        let mut tokens: Vec<String> = concept_tokens(query)
            .into_iter()
            .filter(|token| token.len() >= 3)
            .collect();
        tokens.sort();
        tokens.dedup();
        tokens.truncate(CONCEPT_TOKENS);
        ensure!(
            !tokens.is_empty(),
            "a description needs at least one word of three characters or more to search for"
        );
        let (files, eligible) =
            files_about(&self.workspace, &tokens, self.timeout, self.no_ignore, CONCEPT_FILES)?;
        let read = files.len();
        // A ranking reads definitions, never references, so the seeded index is built the way a
        // snapshot is: call sites and no identifier rows.
        let mut index = StructuralIndex::snapshot(&self.workspace, files, self.timeout)?;
        index.coverage.eligible_files = eligible;
        index.coverage.freshness = "Resolved for this question: the repository was searched for \
                                    the description's own words and the files that carry most of \
                                    them were parsed. Nothing is cached between calls."
            .into();
        index.coverage.limitations = format!(
            "{} This ranking read the {read} of {eligible} source files that carry most of this \
             description's words, so it ranks the corpus's most plausible neighbourhood rather \
             than all of it; a definition that shares none of the query's words is not ranked.",
            index.coverage.limitations
        );
        Ok(index)
    }

    /// Neighbours to try for a name the corpus does not define.
    ///
    /// A wrong guess is the one case where an empty page is worth spending a second search on: the
    /// agent has nothing to recover with, and `search_exact` on a name nothing writes returns
    /// nothing either. The name's own parts are the search - `folio_alloc` looks for `folio` and
    /// `alloc` - and the definitions of the files carrying them are the candidate neighbourhood,
    /// scored by the same `nearest_names` a snapshot used. It runs only on an unknown symbol, so
    /// an answered question never pays for it.
    fn suggest(&self, name: &str, status: &str, nearest: &mut Vec<String>) {
        if status != "unknown_symbol" {
            return;
        }
        let mut parts: Vec<String> = concept_tokens(name)
            .into_iter()
            .filter(|token| token.len() >= 3 && token != name)
            .collect();
        parts.sort();
        parts.dedup();
        parts.truncate(CONCEPT_TOKENS);
        if parts.is_empty() {
            return;
        }
        let Ok((files, _)) =
            files_about(&self.workspace, &parts, self.timeout, self.no_ignore, CONCEPT_FILES)
        else {
            return;
        };
        if let Ok(neighbourhood) =
            StructuralIndex::snapshot(&self.workspace, files, self.timeout)
        {
            *nearest = neighbourhood.nearest_names(name);
        }
    }
}

impl StructuralBackend for Retrieval {
    fn inspect_symbol(&self, workspace: &Workspace, args: InspectArgs) -> Result<InspectResult> {
        self.scan(&[args.name.as_str()])?.inspect_symbol(workspace, args)
    }

    fn find_symbol(
        &self,
        workspace: &Workspace,
        args: SymbolArgs,
    ) -> Result<StructuralResult<Symbol>> {
        let name = args.name.clone();
        let mut found = self.scan(&[name.as_str()])?.find_symbol(workspace, args)?;
        self.suggest(&name, &found.symbol_status, &mut found.nearest_indexed_names);
        Ok(found)
    }

    fn find_callers(&self, workspace: &Workspace, args: CallerArgs) -> Result<CallersResult> {
        let name = args.name.clone();
        let mut found = self.scan(&[name.as_str()])?.find_callers(workspace, args)?;
        self.suggest(&name, &found.retrieval.symbol_status,
                     &mut found.retrieval.nearest_indexed_names);
        Ok(found)
    }

    /// A trace reaches names the question never wrote, so one scan cannot seed it. Each round
    /// scans for every name the previous round's edges named and re-traces over the union, which
    /// converges: a hop adds names or it adds nothing, and the loop stops on either, bounded by
    /// the depth the caller asked for.
    fn trace_dependencies(
        &self,
        workspace: &Workspace,
        args: TraceArgs,
    ) -> Result<DependencyTraceResult> {
        let mut names: BTreeSet<String> = BTreeSet::from([args.name.clone()]);
        let rounds = args.depth.unwrap_or(3).clamp(1, 5);
        let mut traced = {
            let seeds: Vec<&str> = names.iter().map(String::as_str).collect();
            self.scan(&seeds)?.trace_dependencies(workspace, args.clone())?
        };
        for _ in 1..rounds {
            let mut grown = names.clone();
            for edge in &traced.page.results {
                grown.insert(edge.caller.clone());
                grown.insert(edge.callee.clone());
            }
            if grown == names {
                break;
            }
            names = grown;
            let seeds: Vec<&str> = names.iter().map(String::as_str).collect();
            traced = self.scan(&seeds)?.trace_dependencies(workspace, args.clone())?;
        }
        self.suggest(&args.name, &traced.symbol_status, &mut traced.nearest_indexed_names);
        Ok(traced)
    }

    /// Ranking needs definitions, and the description's own words choose whose: the files carrying
    /// most of them are parsed and their definitions ranked.
    fn search_concept(
        &self,
        workspace: &Workspace,
        query: &str,
        path: Option<&str>,
        wanted: usize,
    ) -> Result<Ranked> {
        self.about(query)?.search_concept(workspace, query, path, wanted)
    }

    /// The definition a retrieved line sits inside. One file is parsed to label one hit, because
    /// indexing a repository to name a line would cost more than every answer it labels.
    fn locate(&self, path: &str, line: usize) -> Option<SymbolLocation> {
        StructuralIndex::snapshot(&self.workspace, vec![path.to_owned()], self.timeout)
            .ok()?
            .locate(path, line)
    }

    /// One file answers this: whether the definition at `line` calls any of the other candidates.
    fn calls_among(&self, path: &str, line: usize, names: &[String]) -> Vec<String> {
        StructuralIndex::from_files(&self.workspace, vec![path.to_owned()], self.timeout)
            .map(|index| index.calls_among(path, line, names))
            .unwrap_or_default()
    }

    /// There is no standing index to describe, and saying "zero files indexed" without saying why
    /// would read as an empty corpus. Every page of rows carries its own coverage: how many files
    /// that answer read, out of how many the repository holds.
    fn coverage(&self) -> &Coverage {
        &self.standing
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

/// `struct inode *inode` in a parameter list declares nothing.
///
/// The C grammar spells a type *reference* and a type *definition* with the same node: both are
/// `struct_specifier`, and only the definition carries a body. Treating both as definitions named
/// every parameter, field and local after its type, which on Linux 6.12 is almost everything:
/// **90% of the rows `search_concept` returned over the kernel suite were these**, and 17 of 21
/// rank-1 rows. Worse than noise, they mislabel the right answer - the row for
/// `fs/inode.c:2266`, which is `int file_update_time(struct file *file)`, came back named `file`,
/// so an agent that ranked the correct line was told the wrong symbol and had to spend another
/// call to learn the name. Same family as the iteration-macro and pointer-return-type defects:
/// the grammar's shape, not the text, decides what declares something.
fn type_reference(language: SourceLanguage, node: Node<'_>) -> bool {
    language.family() == LanguageFamily::CFamily
        && matches!(
            node.kind(),
            "struct_specifier" | "union_specifier" | "enum_specifier" | "class_specifier"
        )
        && node.child_by_field_name("body").is_none()
}

/// `for_each_online_node(nid) { ... }` is a macro that expands to a loop, and the C grammar
/// reads the line as a function definition: type `for_each_online_node`, declarator `(nid)`,
/// body the loop. Nothing there declares a function - a real definition's declarator holds a
/// `function_declarator` carrying its parameter list - so the block defines no symbol, and the
/// calls inside it belong to the function that writes the loop. Linux writes that shape in
/// 21,136 of its 34,657 `.c` files; reading it literally invented the symbol `nid` and made a
/// loop variable the caller of everything inside the loop.
///
/// The test is the declarator, not the nesting: refusing every C definition written inside
/// another one also dropped LevelDB's `Status::NotSupported` and a true Lua caller row, because
/// both sit inside regions the grammar had already mis-parsed into one function.
fn macro_block(language: SourceLanguage, node: Node<'_>) -> bool {
    if language.family() != LanguageFamily::CFamily || node.kind() != "function_definition" {
        return false;
    }
    node.child_by_field_name("declarator")
        .and_then(|declarator| first_descendant(declarator, &["function_declarator"]))
        .is_none()
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
            && !type_reference(language, parent)
            || (language == SourceLanguage::Rust && parent.kind() == "impl_item"))
            && holds_definition(language, parent)
            && !macro_block(language, parent)
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
            && !macro_block(source_language, *node)
            && !type_reference(source_language, *node)
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
    let spelled = excerpt(node_text(name, file.source), 200);
    // An unqualified call writes its own name as the expression; saying it twice costs a field
    // in every row of every page for the whole session.
    let expression = if expression == spelled { "" } else { expression };
    Reference {
        name: spelled,
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

    /// The scan honors ignore rules by default and `--no-ignore` reopens them, so a structural
    /// answer and `search_exact` keep describing the same corpus in both modes.
    #[test]
    fn a_scan_honors_ignore_rules_unless_told_otherwise() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join(".ignore"), "skip.rs\n").unwrap();
        std::fs::write(dir.path().join("kept.rs"), "fn kept() {}\n").unwrap();
        std::fs::write(dir.path().join("skip.rs"), "fn skipped() {}\n").unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let found = |no_ignore| {
            Retrieval::new(ws.clone(), Duration::from_secs(5), no_ignore)
                .find_symbol(
                    &ws,
                    SymbolArgs { name: "skipped".into(), path: None, limit: None, offset: None },
                )
                .unwrap()
        };
        let honoring = found(false);
        assert_eq!(honoring.symbol_status, "unknown_symbol");
        assert_eq!(honoring.coverage.eligible_files, 1, "the ignored file is not even counted");
        let reopened = found(true);
        assert_eq!(reopened.symbol_status, "indexed");
        assert_eq!(reopened.page.results[0].path, "skip.rs");
        assert_eq!(reopened.coverage.eligible_files, 2);
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
            .unwrap()
            .regions;
        assert_eq!(hits[0].path, "fuse.rs");
        assert_eq!(hits[0].start_line, 2, "region is the definition, not the comment");
    }

    #[test]
    fn a_call_qualified_by_an_imported_package_names_only_that_package() {
        // `alias.Helper()` in a module whose root is the corpus resolves to one definition even
        // though two files define the name, and the alias is not the directory it names.
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("go.mod"), "module example.com/m\n\ngo 1.24\n").unwrap();
        for (directory, body) in [
            ("real", "package real\n\nfunc Helper() {}\n"),
            ("decoy", "package decoy\n\nfunc Helper() {}\n"),
        ] {
            std::fs::create_dir(dir.path().join(directory)).unwrap();
            std::fs::write(dir.path().join(directory).join("lib.go"), body).unwrap();
        }
        std::fs::write(
            dir.path().join("use.go"),
            "package m\n\nimport aliased \"example.com/m/real\"\n\nfunc Run() { aliased.Helper() }\n",
        )
        .unwrap();
        let files = vec![
            "real/lib.go".to_string(),
            "decoy/lib.go".to_string(),
            "use.go".to_string(),
        ];

        let ws = Workspace::new(dir.path()).unwrap();
        let index =
            StructuralIndex::from_files(&ws, files.clone(), Duration::from_secs(5)).unwrap();
        let callers = index
            .find_callers(
                &ws,
                CallerArgs {
                    name: "Helper".into(),
                    path: None,
                    include_references: None,
                    limit: None,
                    offset: None,
                },
            )
            .unwrap();
        let hit = &callers.retrieval.page.results[0];
        assert_eq!(hit.reference.caller.as_deref(), Some("Run"));
        // Uniform across the page, so the page states it and the rows stay silent.
        assert_eq!(callers.candidate_count, Some(1), "the decoy is not in `real`");
        assert_eq!(callers.resolution.as_deref(), Some("unique_name_candidate"));
        assert_eq!(hit.candidate_count, None);
        assert_eq!(hit.resolution, None);

        // A corpus that is not a module root claims nothing: both definitions stay candidates,
        // because dropping one would cost a real caller to save an ambiguous label.
        std::fs::remove_file(dir.path().join("go.mod")).unwrap();
        let index = StructuralIndex::from_files(&ws, files, Duration::from_secs(5)).unwrap();
        let callers = index
            .find_callers(
                &ws,
                CallerArgs {
                    name: "Helper".into(),
                    path: None,
                    include_references: None,
                    limit: None,
                    offset: None,
                },
            )
            .unwrap();
        assert_eq!(callers.candidate_count, Some(2));
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
                r.resolution.as_deref() == Some("ambiguous") && r.candidate_count == Some(2)
            } else {
                r.resolution.as_deref() == Some("unresolved") && r.candidate_count == Some(0)
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
        assert_eq!(unknown.resolution.as_deref(), Some("unresolved"));
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
            .unwrap()
            .regions;
        assert_eq!(hits[0].path, "text.rs");
        assert_eq!(hits[0].start_line, 2, "{hits:?}");
        assert!(hits[0].score > 0.0);
        // Scope restricts candidates; an unrelated file ranks nothing for this query.
        let scoped = index
            .search_concept(&ws, "wrap lines to a maximum width", Some("other.rs"), 5)
            .unwrap()
            .regions;
        assert!(scoped.is_empty(), "{scoped:?}");
        let checksum = index.search_concept(&ws, "checksum of bytes", None, 1).unwrap().regions;
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
        // An unqualified call spells its own name, so `expression` is left empty and the row
        // carries the name once. A qualified call keeps its expression - the Go package
        // resolution in `qualified_package_dir` reads it.
        assert_eq!(calls[0].expression, "");
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
            .unwrap()
            .regions;
        assert!(index.locate(&hits[0].path, hits[0].start_line).unwrap().symbol.ends_with("::handler"));
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
        assert!(leaf.symbol.ends_with("::leaf"));
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
        // A row states its resolution only when it differs from the page's, so the effective
        // value is the row's if present and the page's otherwise.
        let page_resolution = found.resolution.clone();
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
                    hit.resolution.clone().or_else(|| page_resolution.clone()).unwrap_or_default()
                )
            })
            .collect();
        rows.sort();
        rows
    }

    /// The whole promise of the scan backend: it covers a repository the snapshot cannot hold, and
    /// on one the snapshot *can* hold it answers identically. Rows, resolutions, definitions and
    /// counts are the snapshot's own code; only what was read differs, and coverage says so.
    #[test]
    fn a_scanned_answer_matches_the_snapshot_row_for_row() {
        let (dir, workspace, index) = snapshot(&[
            ("src/util.py", "def normalize(value):\n    return value\n"),
            (
                "src/service.py",
                "from util import normalize\n\n\
                 def handle(row):\n\
                 \treturn normalize(row)\n\n\
                 class Loader:\n\
                 \tdef load(self, row):\n\
                 \t\treturn normalize(row)\n",
            ),
            ("src/unrelated.py", "def spin():\n    return 1\n"),
            ("docs/notes.txt", "normalize is described here\n"),
        ]);
        let scan = Retrieval::new(workspace.clone(), Duration::from_secs(5), false);
        let args = CallerArgs {
            name: "normalize".into(),
            path: None,
            include_references: None,
            limit: None,
            offset: None,
        };
        let held = index.find_callers(&workspace, args.clone()).unwrap();
        let scanned = scan.find_callers(&workspace, args).unwrap();
        assert_eq!(
            serde_json::to_value(&scanned.retrieval.page).unwrap(),
            serde_json::to_value(&held.retrieval.page).unwrap()
        );
        assert_eq!(
            serde_json::to_value(&scanned.candidate_definitions).unwrap(),
            serde_json::to_value(&held.candidate_definitions).unwrap()
        );
        assert_eq!(scanned.retrieval.symbol_status, held.retrieval.symbol_status);
        // And the coverage tells the truth about what each one read: the snapshot parsed every
        // source file, the scan parsed the two that write the name, and both counted the same
        // three eligible files - the text file is not one.
        assert_eq!(held.retrieval.coverage.indexed_files, 3);
        assert_eq!(scanned.retrieval.coverage.indexed_files, 2);
        assert_eq!(scanned.retrieval.coverage.eligible_files, 3);
        assert!(!scanned.retrieval.coverage.budget_truncated);
        assert!(scanned.retrieval.coverage.limitations.contains("2 of 3 source files"));
        drop(dir);
    }

    /// Identifier references are three quarters of an index's records and one optional flag reads
    /// them, so a snapshot does not build them. The flag still has to answer exactly what it
    /// always answered, which it does by scanning the files that write the name.
    #[test]
    fn a_snapshot_drops_identifier_references_and_the_flag_still_answers_them() {
        let files = vec!["util.py".to_string(), "service.py".to_string()];
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("util.py"), "def normalize(value):\n    return value\n")
            .unwrap();
        std::fs::write(
            dir.path().join("service.py"),
            "from util import normalize\n\n\
             def handle(row):\n\
             \treturn normalize(row)\n\n\
             def pick(flag):\n\
             \treturn normalize if flag else None\n",
        )
        .unwrap();
        let workspace = Workspace::new(dir.path()).unwrap();
        let full =
            StructuralIndex::from_files(&workspace, files.clone(), Duration::from_secs(5)).unwrap();
        let held = StructuralIndex::snapshot(&workspace, files, Duration::from_secs(5)).unwrap();
        assert!(full.references.iter().any(|row| row.kind != "call"), "the parser found some");
        assert!(held.references.iter().all(|row| row.kind == "call"), "the snapshot kept none");
        // Asked directly, the snapshot refuses rather than returning the smaller set as an answer.
        let args = CallerArgs {
            name: "normalize".into(),
            path: None,
            include_references: Some(true),
            limit: None,
            offset: None,
        };
        assert!(held.find_callers(&workspace, args.clone()).is_err());
        // Through the backend, in the mode that prefers the snapshot, the flag is scanned for and
        // returns what a full index returns - the call in `handle` and the bare name in `pick`.
        let backend = Retrieval::new(workspace.clone(), Duration::from_secs(5), false);
        let routed = backend.find_callers(&workspace, args.clone()).unwrap();
        let expected = full.find_callers(&workspace, args).unwrap();
        assert_eq!(
            serde_json::to_value(&routed.retrieval.page).unwrap(),
            serde_json::to_value(&expected.retrieval.page).unwrap()
        );
        let owners: Vec<_> = routed
            .retrieval
            .page
            .results
            .iter()
            .map(|hit| hit.reference.caller.clone().unwrap_or_default())
            .collect();
        assert_eq!(owners, vec!["handle".to_string(), "pick".to_string()]);
    }

    /// A name the corpus never writes is answered, not crashed into: the scan finds no candidate
    /// file, parses nothing, and the empty index reports the unknown symbol the snapshot would.
    #[test]
    fn a_scan_for_a_name_nothing_writes_reports_an_unknown_symbol() {
        let (_dir, workspace, _index) =
            snapshot(&[("src/util.py", "def normalize(value):\n    return value\n")]);
        let scan = Retrieval::new(workspace.clone(), Duration::from_secs(5), false);
        let found = scan
            .find_callers(
                &workspace,
                CallerArgs {
                    name: "absent_helper".into(),
                    path: None,
                    include_references: None,
                    limit: None,
                    offset: None,
                },
            )
            .unwrap();
        assert_eq!(found.retrieval.symbol_status, "unknown_symbol");
        assert_eq!(found.retrieval.coverage.indexed_files, 0);
        assert_eq!(found.retrieval.coverage.eligible_files, 1);
    }

    /// Ranking a description needs the corpus's definitions, which is the one thing a repository
    /// too large to index cannot supply. The description's own words choose the files instead,
    /// and the answer says how many it read - never zero, which would read as an empty corpus.
    #[test]
    fn a_description_ranks_over_the_files_its_own_words_choose() {
        let (_dir, workspace, _index) = snapshot(&[
            (
                "src/text.rs",
                "/// Wrap a paragraph to a maximum column width.\n\
                 pub fn break_lines(text: &str, width: usize) -> Vec<String> {\n\
                 \tvec![text.into()]\n\
                 }\n",
            ),
            ("src/ledger.rs", "pub fn post_entry(amount: i64) -> i64 {\n\tamount\n}\n"),
            ("src/audit.rs", "pub fn stamp(when: i64) -> i64 {\n\twhen\n}\n"),
        ]);
        let scan = Retrieval::new(workspace.clone(), Duration::from_secs(5), false);
        let ranked = scan
            .search_concept(&workspace, "wrap a paragraph to a maximum width", None, 3)
            .unwrap();
        assert_eq!(ranked.regions[0].path, "src/text.rs");
        assert_eq!(ranked.regions[0].start_line, 2, "{:?}", ranked.regions);
        // One file carries the description's words; the other two are never parsed, and the count
        // reported is what was read rather than what the repository holds.
        assert_eq!(ranked.files, 1);
        // The hit is labelled by the definition it sits in without a snapshot existing.
        assert_eq!(scan.locate("src/text.rs", 2).unwrap().symbol, "src/text.rs::break_lines");
        // A description with no word long enough to search for is refused, not answered emptily.
        assert!(scan.search_concept(&workspace, "a b c", None, 3).is_err());
    }

    /// A page that returns a wrapper and its callee says which is which.
    ///
    /// The kernel suite lost four trials to this: the model was handed two plausible siblings and
    /// asked `find_callers` about `tls_alert_send` where the question described
    /// `tls_handshake_close`, whose last line calls it. The relationship is in the index already.
    #[test]
    fn a_page_states_which_candidate_calls_which() {
        let (_dir, workspace, index) = snapshot(&[(
            "src/tls.c",
            "void tls_alert_send(int level)\n{\n\tsend(level);\n}\n\
             void tls_handshake_close(void)\n{\n\ttls_alert_send(1);\n}\n\
             void unrelated(void)\n{\n\tnothing();\n}\n",
        )]);
        let _ = &workspace;
        let names = vec![
            "tls_alert_send".to_string(),
            "tls_handshake_close".to_string(),
            "unrelated".to_string(),
        ];
        // The wrapper names its callee.
        assert_eq!(index.calls_among("src/tls.c", 5, &names), vec!["tls_alert_send".to_string()]);
        // The callee names nobody, and a definition never names itself.
        assert!(index.calls_among("src/tls.c", 1, &names).is_empty());
        assert!(index.calls_among("src/tls.c", 9, &names).is_empty());
    }

    /// A parameter's type declares nothing, and the function it sits in declares everything.
    ///
    /// The C grammar spells `struct inode *inode` in a parameter list with the same node kind as
    /// `struct inode { ... }`. Indexing both named every C definition after a type it mentions:
    /// on Linux 6.12, 90% of the rows `search_concept` returned were these, 17 of 21 rank-1 rows
    /// were, and the row for `int file_update_time(struct file *file)` came back named `file`.
    #[test]
    fn a_c_type_reference_is_not_a_definition_and_a_real_struct_still_is() {
        let (_dir, workspace, index) = snapshot(&[(
            "src/fs.c",
            "struct inode {\n\tint mode;\n};\n\
             int file_update_time(struct file *file)\n{\n\treturn 0;\n}\n\
             static void helper(struct timespec64 *times)\n{\n\tfile_update_time(0);\n}\n",
        )]);
        let names = |name: &str| {
            index
                .find_symbol(
                    &workspace,
                    SymbolArgs {
                        name: name.into(),
                        path: None,
                        limit: None,
                        offset: None,
                    },
                )
                .map(|found| found.page.results.len())
                .unwrap_or(0)
        };
        // The function is indexed under its own name, not its parameter's type.
        assert_eq!(names("file_update_time"), 1);
        assert_eq!(names("helper"), 1);
        // A struct with a body is a definition; a type named in a parameter list is not.
        assert_eq!(names("inode"), 1, "a struct with a body is a real definition");
        assert_eq!(names("file"), 0, "struct file *file declares no symbol named file");
        assert_eq!(names("timespec64"), 0, "nor does a parameter's type in a declaration");
    }

    /// A rare word decides the file; a word the corpus writes everywhere does not.
    ///
    /// Counting distinct matched tokens ties every file that carries the query's English, and on
    /// Linux 6.12 that was 56,000 of 60,000 files, so path order chose the candidates and the
    /// answer's own file was never read: gold entered the candidate set for 4 of 21 questions,
    /// and 17 of the losses were this selection rather than any ranking below it. Weighting by
    /// how few files carry each token restores 12 of 21 at 86,605 files and 15 of 21 at 928.
    #[test]
    fn a_rare_word_outranks_a_common_one_when_files_are_chosen() {
        // The answer carries one rare identifier and none of the query's filler; sixty decoys
        // carry every common word and nothing rare.
        let mut files = vec![(
            "src/answer.rs".to_string(),
            "pub fn quiesce_bucket(id: u64) -> u64 {\n\tid\n}\n".to_string(),
        )];
        for index in 0..60 {
            files.push((
                format!("src/decoy{index}.rs"),
                format!("pub fn handle_request_value{index}() {{\n\tlet request = value;\n}}\n"),
            ));
        }
        let owned: Vec<(&str, &str)> =
            files.iter().map(|(path, text)| (path.as_str(), text.as_str())).collect();
        let (_dir, workspace, _index) = snapshot(&owned);
        let scan = Retrieval::new(workspace.clone(), Duration::from_secs(5), false);
        let ranked = scan
            .search_concept(&workspace, "handle the request value that quiesce bucket", None, 5)
            .unwrap();
        assert_eq!(ranked.regions[0].path, "src/answer.rs", "{:?}", ranked.regions);
    }

    /// The per-file line budget decides what the score is allowed to see.
    ///
    /// A long file whose first lines carry only the query's common words used to exhaust a
    /// 32-line budget before reaching its one rare line, which is precisely the shape of a kernel
    /// source file, and the file was then scored as if it never carried the word.
    #[test]
    fn a_rare_word_deep_in_a_long_file_is_still_seen() {
        let mut long = String::new();
        for index in 0..400 {
            long.push_str(&format!("// request value handler line {index}\n"));
        }
        long.push_str("pub fn quiesce_bucket(id: u64) -> u64 {\n\tid\n}\n");
        let (_dir, workspace, _index) = snapshot(&[
            ("src/long.rs", long.as_str()),
            ("src/other.rs", "pub fn request_value() {\n\tlet request = 1;\n}\n"),
        ]);
        let scan = Retrieval::new(workspace.clone(), Duration::from_secs(5), false);
        let ranked = scan
            .search_concept(&workspace, "quiesce the bucket identifier", None, 5)
            .unwrap();
        assert_eq!(ranked.regions.first().map(|region| region.path.as_str()), Some("src/long.rs"),
                   "{:?}", ranked.regions);
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

    /// A C iteration macro takes its body in braces, so the grammar reads
    /// `for_each_online_node(nid) { ... }` as a function definition declaring `nid`. C cannot
    /// nest a function definition inside a body, so the block is a macro: `nid` is no definition
    /// and the calls inside the loop belong to the function that writes it. Linux writes this
    /// shape in 21,136 of its 34,657 `.c` files, and reading it literally invented a symbol and
    /// named a loop variable as the caller of everything in the loop.
    #[test]
    fn a_c_macro_block_is_not_a_definition_and_owns_no_calls() {
        let (_dir, workspace, index) = snapshot(&[
            (
                "mm/cma.c",
                "void cma_declare_contiguous_nid(unsigned long size, int nid) {}\n",
            ),
            (
                "mm/hugetlb.c",
                "#include \"cma.h\"\n\n\
                 void hugetlb_cma_reserve(int order) {\n\
                 \tint nid;\n\
                 \tfor_each_online_node(nid) {\n\
                 \t\tcma_declare_contiguous_nid(order, nid);\n\
                 \t}\n\
                 }\n",
            ),
        ]);
        assert!(!index.symbols.contains_key("nid"), "{:?}", index.symbols.keys());
        assert_eq!(
            caller_rows(&index, &workspace, "cma_declare_contiguous_nid"),
            vec!["mm/hugetlb.c::hugetlb_cma_reserve:unique_name_candidate".to_string()]
        );
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
            .unwrap()
            .regions;
        assert!(index.locate(&hits[0].path, hits[0].start_line).unwrap().symbol.ends_with("::clampWidth"));
        assert!(
            index.symbols.contains_key("report"),
            "the namespace itself stays indexed as a definition"
        );
    }
}

