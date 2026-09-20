use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeSet, path::PathBuf, time::Duration};

/// Every tool this server can expose, in the order `tools/list` reports them.
pub const TOOLS: [&str; 7] = [
    "search_exact",
    "read_source",
    "inspect_symbol",
    "find_symbol",
    "find_callers",
    "trace_dependencies",
    "search_concept",
];

/// What a session exposes when the operator restricts nothing.
///
/// This is not every tool: it is the set that survived a measured pruning rule, which admits a
/// tool only when it has a task class a bounded search-and-read crawl cannot dissolve *and* saves
/// more context than the schema it re-sends on every turn. `find_callers` cleared that by a wide
/// margin on exhaustive caller questions; `inspect_symbol` and `find_symbol` were each given their
/// own claimed class over three repetitions and returned negative ledgers with no unique solves;
/// `trace_dependencies` was never called at all across three separate experiments, including on
/// questions authored for it, while carrying the largest schema of any tool. Those three remain
/// available through `--tools` and `--profile`, so nothing is removed - only un-defaulted.
pub const DEFAULT_SURFACE: [&str; 4] = [
    "search_exact",
    "read_source",
    "find_callers",
    "search_concept",
];

/// The availability levels of the original routing study, kept so its recorded commands still
/// run. They are presets over `--tools`, not a separate mechanism.
pub const PROFILES: [(&str, &[&str]); 4] = [
    ("A", &["search_exact", "read_source"]),
    (
        "B",
        &[
            "search_exact",
            "read_source",
            "inspect_symbol",
            "find_symbol",
            "find_callers",
            "trace_dependencies",
        ],
    ),
    ("C", &["search_exact", "read_source", "search_concept"]),
    ("D", &TOOLS),
];

/// Which implementation answers `search_concept`. This is an operator choice: the model sees one
/// conceptual-search tool and never picks a ranking mechanism.
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Ranker {
    #[default]
    Lexical,
    Semantic,
    Hybrid,
}

impl Ranker {
    pub fn needs_backend(self) -> bool {
        matches!(self, Self::Semantic | Self::Hybrid)
    }
    pub fn needs_index(self) -> bool {
        matches!(self, Self::Lexical | Self::Hybrid)
    }
}

/// How a name-addressed structural question is answered. Also an operator choice: the model sees
/// the same tools and the same rows either way, and the coverage block says which mode answered.
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Structural {
    /// Snapshot while the whole repository fits inside `Budget`, candidate scan once it does not.
    /// A truncated snapshot answers every caller question partially and says so, which is honest
    /// and useless; a scan answers the same question over the whole repository.
    #[default]
    Auto,
    /// Always the whole-repository snapshot, partial when the budget binds.
    Snapshot,
    /// Always the candidate scan. Exists so a measurement can put both modes on one corpus.
    Scan,
}

#[derive(Clone, Debug)]
pub struct Config {
    /// The repository this session may read, when the operator pinned one.
    ///
    /// `None` means "ask the client": a stdio server is launched inside the project the user
    /// opened, and the client already knows that directory, so a per-project configuration entry
    /// spelling it out again is a copy that can go stale. The root is then resolved from the
    /// client's own roots on the first tool call. An explicit `--root` still wins, and is the only
    /// way to point a session at a directory the client did not open.
    pub root: Option<PathBuf>,
    /// Exactly the tools this session exposes; anything else is absent and uncallable.
    pub tools: BTreeSet<String>,
    /// What the invocation log calls this tool set: a profile letter, or the tools themselves.
    pub label: String,
    pub semantic_command: Option<Vec<String>>,
    pub timeout: Duration,
    pub run_id: Option<String>,
    pub log_file: Option<PathBuf>,
    pub ranker: Ranker,
    /// Search and index gitignored/ignored files too. Off by default: ignore files usually
    /// exclude generated code and secrets, and every measured run kept them excluded. The flag
    /// exists for repositories whose ignore rules hide the code under study (for example a
    /// nested repository ignored by its parent). Hidden files and `.git` stay excluded.
    pub no_ignore: bool,
    /// Snapshot, candidate scan, or the size-dependent choice between them.
    pub structural_mode: Structural,
}

impl Config {
    pub fn enabled(&self, tool: &str) -> bool {
        self.tools.contains(tool)
    }
    pub fn structural(&self) -> bool {
        ["inspect_symbol", "find_symbol", "find_callers", "trace_dependencies"]
            .iter()
            .any(|tool| self.enabled(tool))
    }
    pub fn semantic(&self) -> bool {
        self.enabled("search_concept")
    }

    pub fn parse() -> Result<Option<Self>> {
        let mut args = std::env::args().skip(1);
        let mut config = Self {
            root: None,
            tools: DEFAULT_SURFACE.iter().map(|tool| (*tool).to_owned()).collect(),
            label: "default".into(),
            semantic_command: None,
            timeout: Duration::from_secs(30),
            run_id: None,
            log_file: None,
            ranker: Ranker::default(),
            no_ignore: false,
            structural_mode: Structural::default(),
        };
        // One switch decides the tool set; two would leave the log label ambiguous.
        let mut chosen = false;
        while let Some(flag) = args.next() {
            if flag == "--help" || flag == "-h" {
                println!(
                    "retrieval-mcp [--root PATH] [--tools name,name,...] [--profile A|B|C|D]\n  [--ranker lexical|semantic|hybrid] [--structural auto|snapshot|scan] [--run-id ID]\n  [--semantic-command '[\"program\",\"arg\"]'] [--timeout-seconds 30]\n  [--log-file /absolute/path/events.jsonl] [--no-ignore]\n--root pins the only repository the session can read. Without it, the first tool call takes\n  the client's first reported root, or the directory the client started this server in when\n  it reports none, so a client that opens a project needs no path in its configuration. A\n  home directory or the filesystem root is refused instead of indexed.\n--structural decides how a name-addressed question is answered: snapshot builds one index of\n  the whole repository, scan searches the repository for the name and parses only the files\n  that write it, and auto - the default - takes the snapshot while it covers the repository\n  and the scan once the index budget would truncate it.\n--no-ignore searches and indexes files that ignore files exclude; .git and hidden files stay out.\nTools: search_exact, read_source, inspect_symbol, find_symbol, find_callers,\n  trace_dependencies, search_concept.\nWithout --tools or --profile the default surface is search_exact, read_source,\n  find_callers and search_concept: the tools that measurably repaid their schema cost.\n  The other three stay available by naming them, or with --profile D.\nProfiles are presets over --tools from the original availability study: A exact+read,\n  B adds structure, C adds concept search, D all seven.\nJSON invocation logs go to stderr and optionally append to --log-file; stdout is reserved for MCP."
                );
                return Ok(None);
            }
            // Every install path asks a binary what it is: release scripts, `brew`, a user
            // checking which build a client launched. Without this, `retrieval-mcp --version`
            // read the flag as one expecting a value and answered "missing value for --version",
            // which is the shape of an answer nobody can act on.
            if flag == "--version" || flag == "-V" {
                println!("retrieval-mcp {}", env!("CARGO_PKG_VERSION"));
                return Ok(None);
            }
            if flag == "--no-ignore" {
                config.no_ignore = true;
                continue;
            }
            let value = args
                .next()
                .with_context(|| format!("missing value for {flag}"))?;
            match flag.as_str() {
                "--root" => config.root = Some(value.into()),
                "--log-file" => config.log_file = Some(value.into()),
                "--tools" => {
                    let requested: Vec<String> =
                        value.split(',').map(|tool| tool.trim().to_owned()).collect();
                    ensure!(
                        requested.iter().all(|tool| TOOLS.contains(&tool.as_str())),
                        "--tools accepts only: {}",
                        TOOLS.join(", ")
                    );
                    ensure!(!requested.is_empty(), "--tools needs at least one tool");
                    ensure!(!chosen, "use either --tools or --profile, not both");
                    config.label = requested.join("+");
                    config.tools = requested.into_iter().collect();
                    chosen = true;
                }
                "--profile" => {
                    let (name, preset) = PROFILES
                        .iter()
                        .find(|(name, _)| *name == value)
                        .with_context(|| format!("profile must be A, B, C, or D, not {value}"))?;
                    ensure!(!chosen, "use either --tools or --profile, not both");
                    config.label = (*name).to_owned();
                    config.tools = preset.iter().map(|tool| (*tool).to_owned()).collect();
                    chosen = true;
                }
                "--ranker" => {
                    config.ranker = serde_json::from_value(serde_json::Value::String(value))
                        .context("ranker must be lexical, semantic, or hybrid")?
                }
                "--structural" => {
                    config.structural_mode =
                        serde_json::from_value(serde_json::Value::String(value))
                            .context("structural must be auto, snapshot, or scan")?
                }
                "--semantic-command" => {
                    let command: Vec<String> = serde_json::from_str(&value)
                        .context("semantic command must be a JSON string array")?;
                    ensure!(
                        !command.is_empty() && !command[0].is_empty(),
                        "semantic command cannot be empty"
                    );
                    config.semantic_command = Some(command);
                }
                "--timeout-seconds" => {
                    let seconds: u64 = value.parse().context("timeout must be an integer")?;
                    ensure!(
                        (1..=600).contains(&seconds),
                        "timeout must be 1..600 seconds"
                    );
                    config.timeout = Duration::from_secs(seconds);
                }
                "--run-id" => {
                    ensure!(value.len() <= 256, "run ID too long");
                    config.run_id = Some(value);
                }
                _ => bail!("unknown option: {flag}; use --help"),
            }
        }
        Ok(Some(config))
    }
}