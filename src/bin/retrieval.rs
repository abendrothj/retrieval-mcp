//! The same four retrieval operations as a command, for the CLI transport arm.
//!
//! `runs/cli-transport-20260924` asks whether this server's round trips are a property of the
//! channel. A shell call carries a measured 1.93 to 1.95 sub-commands at both corpus scales while
//! an MCP call carries one by construction, and `runs/locbench-noshell-20260923` showed that
//! taking the shell away does not reduce requests - it converts them. So the question is whether
//! the same index, reached as a command the agent can pipe, costs fewer round trips.
//!
//! Three things here are load-bearing for that comparison.
//!
//! Every call goes through `RetrievalServer::run_once`, which is the same `execute` an MCP tool
//! call uses. There is no second implementation to drift, and a differential test asserts both
//! channels answer identically.
//!
//! Output is *lossless* - every field the MCP payload carries is emitted - but split by stream:
//! tab-separated rows on stdout, coverage and counts on stderr as `# key=value`. Lossless because
//! a trimmed projection would make the arm a comparison of two payloads as well as two channels.
//! Split because with the scalars printed first on stdout, `retrieval callers X | head` returned
//! 41 lines of which the first 31 were coverage prose: the most natural pipe an agent writes
//! produced no rows, and an arm measured that way would have reported a broken renderer as a fact
//! about transport. Codex's aggregated_output captures both streams, verified against the
//! archive, so nothing is hidden from the model - it can now choose not to pipe it. `--json`
//! emits the payload verbatim on stdout.
//!
//! Exit status follows grep, because that is what `&&` chaining and the model's priors expect:
//! 0 rows found, 1 none found, 2 error.
use anyhow::{Context, Result, bail};
use retrieval_mcp::config::{Config, DEFAULT_SURFACE, Ranker};
use retrieval_mcp::tools::RetrievalServer;
use serde_json::{Map, Value, json};
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::time::Duration;

const USAGE: &str = "\
retrieval [--root PATH] [--json] <command> [options]

  search <pattern>      Literal or regex text search. Repeat -e for several patterns in one call.
                        [-e PATTERN] [--path P] [--regex] [--case-sensitive] [--limit N] [--offset N]
  read <path>           Read source lines. [--start-line N] [--end-line N]
  callers <name>        Direct call sites for an unqualified symbol.
                        [--path P] [--include-references] [--limit N] [--offset N]
  concept <query>       Find code by described behaviour. [--path P] [--limit N] [--offset N] [--excerpt]

Rows go to stdout, tab-separated, one per line, so they pipe. Coverage and counts go to stderr
as `# key=value`, visible in a terminal and out of the way of a pipe. --json emits
the payload unchanged. Exit status: 0 rows found, 1 none found, 2 error.
Paths are relative to --root, which defaults to the working directory.";

/// The tool set the MCP arm exposes, so the two channels differ in transport and nothing else.
fn config(root: PathBuf) -> Config {
    Config {
        root: Some(root),
        tools: DEFAULT_SURFACE.iter().map(|tool| (*tool).to_owned()).collect::<BTreeSet<_>>(),
        label: "cli".into(),
        semantic_command: None,
        timeout: Duration::from_secs(30),
        run_id: None,
        log_file: None,
        ranker: Ranker::default(),
        no_ignore: false,
    }
}

struct Args {
    flags: Vec<(String, Option<String>)>,
    positional: Vec<String>,
}

impl Args {
    /// One value for a flag, or None. Repeated flags are kept in order for `-e`.
    fn take(&self, name: &str) -> Option<&str> {
        self.flags.iter().find(|(f, _)| f == name).and_then(|(_, v)| v.as_deref())
    }
    fn present(&self, name: &str) -> bool {
        self.flags.iter().any(|(f, _)| f == name)
    }
    fn all(&self, name: &str) -> Vec<String> {
        self.flags
            .iter()
            .filter(|(f, _)| f == name)
            .filter_map(|(_, v)| v.clone())
            .collect()
    }
    fn number(&self, name: &str) -> Result<Option<usize>> {
        match self.take(name) {
            None => Ok(None),
            Some(raw) => Ok(Some(
                raw.parse().with_context(|| format!("{name} needs a number, not {raw}"))?,
            )),
        }
    }
}

/// Flags that take no value. Everything else consumes the next argument, which mirrors how
/// `Config::parse` reads the server's own flags.
const BARE: [&str; 5] = ["--json", "--regex", "--case-sensitive", "--include-references", "--excerpt"];

fn parse(raw: Vec<String>) -> Result<Args> {
    let mut args = Args { flags: Vec::new(), positional: Vec::new() };
    let mut it = raw.into_iter();
    while let Some(token) = it.next() {
        if !token.starts_with('-') || token == "-" {
            args.positional.push(token);
            continue;
        }
        if BARE.contains(&token.as_str()) {
            args.flags.push((token, None));
            continue;
        }
        let value = it.next().with_context(|| format!("missing value for {token}"))?;
        args.flags.push((token, Some(value)));
    }
    Ok(args)
}

/// Build the tool arguments. Field names are the MCP schema's, so one validation path serves both.
fn tool_args(command: &str, args: &Args) -> Result<(&'static str, Value)> {
    // positional[0] is the command itself; the subject is what follows it.
    let subject = args.positional.get(1).cloned();
    let mut map = Map::new();
    let mut put = |key: &str, value: Value| {
        if !value.is_null() {
            map.insert(key.to_owned(), value);
        }
    };
    match command {
        "search" => {
            let repeated = args.all("-e");
            match (subject, repeated.is_empty()) {
                (Some(one), true) => put("query", json!(one)),
                (None, false) => put("queries", json!(repeated)),
                (Some(_), false) => bail!("pass a pattern or -e patterns, not both"),
                (None, true) => bail!("search needs a pattern"),
            }
            put("path", json!(args.take("--path")));
            if args.present("--regex") {
                put("regex", json!(true));
            }
            if args.present("--case-sensitive") {
                put("case_sensitive", json!(true));
            }
            put("limit", json!(args.number("--limit")?));
            put("offset", json!(args.number("--offset")?));
            Ok(("search_exact", Value::Object(map)))
        }
        "read" => {
            put("path", json!(subject.context("read needs a path")?));
            put("start_line", json!(args.number("--start-line")?));
            put("end_line", json!(args.number("--end-line")?));
            Ok(("read_source", Value::Object(map)))
        }
        "callers" => {
            put("name", json!(subject.context("callers needs a symbol name")?));
            put("path", json!(args.take("--path")));
            if args.present("--include-references") {
                put("include_references", json!(true));
            }
            put("limit", json!(args.number("--limit")?));
            put("offset", json!(args.number("--offset")?));
            Ok(("find_callers", Value::Object(map)))
        }
        "concept" => {
            put("query", json!(subject.context("concept needs a query")?));
            put("path", json!(args.take("--path")));
            put("limit", json!(args.number("--limit")?));
            put("offset", json!(args.number("--offset")?));
            if args.present("--excerpt") {
                put("fields", json!(["excerpt"]));
            }
            Ok(("search_concept", Value::Object(map)))
        }
        other => bail!("unknown command {other}; one of search, read, callers, concept"),
    }
}

/// A scalar as one field; anything nested as compact JSON so a row never spans two lines.
fn cell(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(text) => text.replace('\t', "    ").replace('\n', "\\n"),
        other => other.to_string(),
    }
}

/// Flatten the payload's scalar fields into `# key=value` lines, depth-first.
fn preamble(prefix: &str, value: &Value, out: &mut Vec<String>) {
    match value {
        Value::Object(fields) => {
            for (key, inner) in fields {
                let path = if prefix.is_empty() { key.clone() } else { format!("{prefix}.{key}") };
                preamble(&path, inner, out);
            }
        }
        Value::Array(_) => {}
        scalar => out.push(format!("# {prefix}={}", cell(scalar))),
    }
}

/// Every row set in the payload, in key order.
///
/// Not "the first array of objects": a `find_callers` result carries both
/// `candidate_definitions` and the caller rows, and picking one by key order silently rendered
/// the definition of the symbol instead of its callers. Emitting all of them, each under its own
/// header, is both lossless and unambiguous about which set is which.
fn rows(payload: &Value) -> Vec<(&String, &Vec<Value>)> {
    let Some(fields) = payload.as_object() else {
        return Vec::new();
    };
    fields
        .iter()
        .filter_map(|(key, value)| match value {
            Value::Array(items) if !items.is_empty() && items.iter().all(Value::is_object) => {
                Some((key, items))
            }
            _ => None,
        })
        .collect()
}

/// Lossless rendering, split by stream: rows for stdout, everything else for stderr.
///
/// The split is not cosmetic. With the scalar fields printed first on stdout,
/// `retrieval callers X | head` returned 41 lines of which the first 31 were `# coverage.*`
/// prose - the most natural pipe an agent writes produced no rows at all, and an arm measured
/// that way would have reported a broken renderer as a fact about transport. Data on stdout and
/// diagnostics on stderr is also just what every other command does, so it costs the agent no
/// new convention. Nothing is dropped: Codex's aggregated_output captures both streams, verified
/// against the archive, so the model still sees the coverage fields - it can now choose not to
/// pipe them.
fn render(payload: &Value) -> (String, String, bool) {
    let mut notes = Vec::new();
    preamble("", payload, &mut notes);
    let mut lines = Vec::new();
    let sets = rows(payload);
    for (name, items) in &sets {
        let mut columns: Vec<String> = Vec::new();
        for item in *items {
            for key in item.as_object().into_iter().flatten().map(|(k, _)| k) {
                if !columns.iter().any(|c| c == key) {
                    columns.push(key.clone());
                }
            }
        }
        lines.push(format!("# {name}: {}", columns.join("\t")));
        for item in *items {
            let fields = item.as_object().expect("rows() admits only objects");
            lines.push(
                columns
                    .iter()
                    .map(|c| cell(fields.get(c).unwrap_or(&Value::Null)))
                    .collect::<Vec<_>>()
                    .join("\t"),
            );
        }
    }
    (lines.join("\n"), notes.join("\n"), !sets.is_empty())
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    match run().await {
        Ok(code) => code,
        Err(error) => {
            eprintln!("retrieval: {error:#}");
            std::process::ExitCode::from(2)
        }
    }
}

async fn run() -> Result<std::process::ExitCode> {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    if raw.is_empty() || raw.iter().any(|a| a == "--help" || a == "-h") {
        println!("{USAGE}");
        return Ok(std::process::ExitCode::SUCCESS);
    }
    if raw.iter().any(|a| a == "--version" || a == "-V") {
        println!("retrieval {}", env!("CARGO_PKG_VERSION"));
        return Ok(std::process::ExitCode::SUCCESS);
    }
    // Flags are read before the command is chosen, so `--root X callers foo` and
    // `callers foo --root X` mean the same thing - which is what a shell user assumes.
    let args = parse(raw)?;
    let command = args.positional.first().context("no command; try --help")?.clone();
    let root = match args.take("--root") {
        Some(path) => PathBuf::from(path),
        None => std::env::current_dir().context("no --root and no working directory")?,
    };
    let (tool, tool_arguments) = tool_args(&command, &args)?;
    let payload = RetrievalServer::run_once(config(root.clone()), &root, tool, tool_arguments)
        .await
        .with_context(|| format!("{command} failed"))?;
    if args.present("--json") {
        println!("{}", serde_json::to_string(&payload)?);
        return Ok(if !rows(&payload).is_empty() {
            std::process::ExitCode::SUCCESS
        } else {
            std::process::ExitCode::from(1)
        });
    }
    let (text, notes, found) = render(&payload);
    if !notes.is_empty() {
        eprintln!("{notes}");
    }
    if !text.is_empty() {
        println!("{text}");
    }
    Ok(if found { std::process::ExitCode::SUCCESS } else { std::process::ExitCode::from(1) })
}
