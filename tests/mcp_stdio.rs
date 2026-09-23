use serde_json::{Value, json};
use std::sync::Arc;
use std::{collections::HashMap, process::Stdio, time::Duration};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
};

struct Client {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    /// Events read off stderr as they are logged, so a test can wait for one the server handles
    /// on its own task instead of in message order.
    logs: Arc<tokio::sync::Mutex<Vec<Value>>>,
    reader: tokio::task::JoinHandle<()>,
    id: u64,
    /// What this client answers `roots/list` with. `None` declares no roots capability at all, so
    /// a server that wants a root has nowhere to ask.
    roots: Option<Vec<String>>,
    /// How many times the server asked for them.
    asked: usize,
    /// Declared the capability and answers nothing, like a client that has hung.
    mute: bool,
    /// The handshake's instructions, which every turn pays for.
    instructions: String,
}

/// A local directory as the `file:` URI a client reports. Spaces are percent-escaped, because
/// that is how a real client reports `~/My Projects` and a server that does not decode it looks
/// for a directory that does not exist.
fn file_uri(path: &std::path::Path) -> String {
    format!("file://{}", path.to_str().unwrap().replace(' ', "%20"))
}

/// The field objects of every logged line naming `event`. An invocation-log record carries its
/// fields at the top level; the server's own tracing events nest theirs under `fields`.
fn logged<'a>(logs: &'a [Value], event: &str) -> Vec<&'a Value> {
    logs.iter()
        .filter_map(|line| {
            let fields = if line["event"].is_string() {
                line
            } else {
                &line["fields"]
            };
            (fields["event"] == event).then_some(fields)
        })
        .collect()
}
impl Client {
    async fn start(root: &std::path::Path, profile: &str) -> Self {
        Self::with_backend(root, profile, None).await
    }
    async fn with_backend(root: &std::path::Path, profile: &str, backend: Option<Value>) -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"));
        command.args([
            "--root",
            root.to_str().unwrap(),
            "--profile",
            profile,
            "--run-id",
            "integration",
            "--timeout-seconds",
            "120",
        ]);
        if let Some(backend) = backend {
            // A backend implies the operator selected the ranker that uses one.
            command.arg("--ranker").arg("semantic");
            command.arg("--semantic-command").arg(backend.to_string());
        }
        Self::spawn(command).await
    }
    /// A client that reports roots, like Claude Code, for a server started without `--root`.
    async fn start_with_roots(roots: &[&std::path::Path], profile: &str) -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"));
        command.args([
            "--profile",
            profile,
            "--run-id",
            "integration",
            "--timeout-seconds",
            "120",
        ]);
        let reported = roots.iter().map(|root| file_uri(root)).collect();
        Self::spawn_declaring(command, Some(reported)).await
    }
    async fn spawn(command: Command) -> Self {
        Self::spawn_declaring(command, None).await
    }
    /// Declares the roots capability and then never answers, which is what a hung or buggy client
    /// looks like from the server's side.
    async fn spawn_mute(command: Command) -> Self {
        let mut client = Self::spawn_declaring(command, Some(Vec::new())).await;
        client.mute = true;
        client
    }
    async fn spawn_declaring(mut command: Command, roots: Option<Vec<String>>) -> Self {
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        let mut stderr = BufReader::new(child.stderr.take().unwrap()).lines();
        let logs: Arc<tokio::sync::Mutex<Vec<Value>>> = Arc::new(tokio::sync::Mutex::new(Vec::new()));
        let collected = Arc::clone(&logs);
        let reader = tokio::spawn(async move {
            while let Some(line) = stderr.next_line().await.unwrap() {
                let event: Value = serde_json::from_str(&line)
                    .unwrap_or_else(|_| panic!("stderr must carry only JSON events: {line}"));
                collected.lock().await.push(event);
            }
        });
        let capabilities = if roots.is_some() {
            json!({"roots": {"listChanged": true}})
        } else {
            json!({})
        };
        let mut client = Self {
            stdin: child.stdin.take().unwrap(),
            stdout: BufReader::new(child.stdout.take().unwrap()),
            child,
            logs,
            reader,
            id: 0,
            roots,
            asked: 0,
            mute: false,
            instructions: String::new(),
        };
        let init = client.request("initialize", json!({"protocolVersion":"2025-11-25","capabilities":capabilities,"clientInfo":{"name":"test","version":"1"}})).await;
        assert!(init.get("result").is_some(), "{init}");
        client.instructions = init["result"]["instructions"].as_str().unwrap_or_default().to_owned();
        client
            .send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}))
            .await;
        client
    }
    /// Answers a server-initiated request, and says whether it did.
    ///
    /// `roots/list` arrives as a request carrying the *server's* id, which can be the same number
    /// as a client request's, so every read loop has to route on `method` before matching ids.
    async fn serve(&mut self, message: &Value) -> bool {
        if message["method"] != "roots/list" {
            return false;
        }
        self.asked += 1;
        if self.mute {
            return true;
        }
        let roots: Vec<Value> = self
            .roots
            .clone()
            .expect("the server asked a client that declared no roots capability")
            .iter()
            .map(|uri| json!({"uri": uri}))
            .collect();
        let id = message["id"].clone();
        self.send(json!({"jsonrpc":"2.0","id":id,"result":{"roots":roots}}))
            .await;
        true
    }
    /// Reports a different set of roots and tells the server they moved.
    async fn move_roots(&mut self, roots: &[&std::path::Path]) {
        self.roots = Some(roots.iter().map(|root| file_uri(root)).collect());
        self.send(json!({"jsonrpc":"2.0","method":"notifications/roots/list_changed"}))
            .await;
    }
    /// Waits until the server has logged `event` at least `times`, so a test can act after a
    /// notification it handles on a task of its own rather than in message order.
    async fn await_event(&self, event: &str, times: usize) {
        let seen = tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                let seen = logged(&self.logs.lock().await, event).len();
                if seen >= times {
                    return;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await;
        assert!(seen.is_ok(), "{event} was not logged {times} time(s)");
    }
    async fn send(&mut self, value: Value) {
        self.stdin
            .write_all(format!("{value}\n").as_bytes())
            .await
            .unwrap();
        self.stdin.flush().await.unwrap();
    }
    async fn request(&mut self, method: &str, params: Value) -> Value {
        self.id += 1;
        self.send(json!({"jsonrpc":"2.0","id":self.id,"method":method,"params":params}))
            .await;
        tokio::time::timeout(Duration::from_secs(130), async {
            loop {
                let mut line = String::new();
                assert!(
                    self.stdout.read_line(&mut line).await.unwrap() > 0,
                    "server closed stdout"
                );
                let value: Value =
                    serde_json::from_str(&line).expect("stdout must contain only MCP JSON");
                if self.serve(&value).await {
                    continue;
                }
                if value["id"] == self.id && value.get("method").is_none() {
                    return value;
                }
            }
        })
        .await
        .expect("MCP response timeout")
    }
    async fn tool(&mut self, name: &str, args: Value) -> Value {
        self.request("tools/call", json!({"name":name,"arguments":args}))
            .await["result"]
            .clone()
    }
    /// Writes a request and returns its id without waiting for the reply, so several calls can be
    /// in flight at once.
    async fn dispatch(&mut self, method: &str, params: Value) -> u64 {
        self.id += 1;
        let id = self.id;
        self.send(json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}))
            .await;
        id
    }
    async fn dispatch_tool(&mut self, name: &str, args: Value) -> u64 {
        self.dispatch("tools/call", json!({"name":name,"arguments":args}))
            .await
    }
    /// Reads replies until every id in `owed` has one. Unlike `request`, which skips whatever it
    /// is not waiting for, this refuses a second reply for an id and a reply for an id nobody
    /// asked for: with several calls in flight, a misrouted or duplicated response is otherwise
    /// silently discarded by whichever reader is not waiting for it.
    async fn collect(&mut self, owed: &[u64]) -> HashMap<u64, Value> {
        let mut answered: HashMap<u64, Value> = HashMap::new();
        let within = tokio::time::timeout(Duration::from_secs(60), async {
            while !owed.iter().all(|id| answered.contains_key(id)) {
                let mut line = String::new();
                assert!(
                    self.stdout.read_line(&mut line).await.unwrap() > 0,
                    "server closed stdout with replies still owed"
                );
                let value: Value =
                    serde_json::from_str(&line).expect("stdout must contain only MCP JSON");
                if self.serve(&value).await {
                    continue;
                }
                // Server-initiated notifications carry no id and answer no request.
                let Some(id) = value["id"].as_u64() else {
                    continue;
                };
                assert!(
                    owed.contains(&id),
                    "a reply arrived for an id nobody asked for: {value}"
                );
                assert!(
                    answered.insert(id, value).is_none(),
                    "id {id} was answered twice"
                );
            }
        })
        .await;
        assert!(
            within.is_ok(),
            "replies never arrived for {:?}",
            owed.iter()
                .filter(|id| !answered.contains_key(id))
                .collect::<Vec<_>>()
        );
        answered
    }
    async fn stop(mut self) -> Vec<Value> {
        drop(self.stdin);
        // Generous on purpose: nineteen of these run at once, each with its own server process
        // and index build, and a five-second budget turned a loaded machine into a failed test
        // that passed on rerun. A hung server still fails here, just later.
        tokio::time::timeout(Duration::from_secs(30), self.child.wait())
            .await
            .expect("the server did not exit after stdin closed")
            .unwrap();
        self.reader.await.unwrap();
        self.logs.lock().await.clone()
    }
}

#[tokio::test]
async fn exact_and_read_over_stdio_with_logs() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(
        root.path().join("sample.rs"),
        "fn needle() {}\nfn caller() { needle(); }\n",
    )
    .unwrap();
    let mut client = Client::start(root.path(), "A").await;
    let listed = client.request("tools/list", json!({})).await;
    assert_eq!(listed["result"]["tools"].as_array().unwrap().len(), 2);
    let search = client
        .tool("search_exact", json!({"query":"needle","limit":1}))
        .await;
    assert_ne!(search["isError"], true, "{search}");
    assert_eq!(
        search["structuredContent"]["results"][0]["path"],
        "sample.rs"
    );
    assert_eq!(search["structuredContent"]["results"][0]["line"], 1);
    assert_eq!(search["structuredContent"]["next_offset"], 1);
    let read = client
        .tool(
            "read_source",
            json!({"path":"sample.rs","start_line":2,"end_line":2}),
        )
        .await;
    assert_eq!(
        read["structuredContent"]["lines"][0]["text"],
        "fn caller() { needle(); }"
    );
    assert_eq!(
        client
            .tool("read_source", json!({"path":"../outside"}))
            .await["isError"],
        true
    );
    assert_eq!(
        client
            .tool("search_exact", json!({"query":"[","regex":true}))
            .await["isError"],
        true
    );
    assert_eq!(
        client
            .tool(
                "read_source",
                json!({"path":"sample.rs","start_line":"bad"})
            )
            .await["isError"],
        true
    );
    let logs = client.stop().await;
    let completed: Vec<_> = logs.iter().filter(|v| v["event"] == "tool_end").collect();
    assert_eq!(completed.len(), 5);
    let fields = completed[0];
    let invocation = fields;
    assert_eq!(invocation["sequence"], 1);
    assert_eq!(invocation["tool"], "search_exact");
    assert_eq!(invocation["run_id"], "integration");
    assert!(fields["response_bytes"].as_u64().unwrap() > 0);
    assert_eq!(fields["result_count"], 1);
    assert!(fields["latency_ms"].is_number());
    assert_eq!(fields["locations"][0]["path"], "sample.rs");
}

#[tokio::test]
async fn profiles_and_structural_queries_over_stdio() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(
        root.path().join("sample.rs"),
        "fn target() {}\nfn caller() { target(); unknown(); }\nfn outer() { caller(); }\n",
    )
    .unwrap();
    for (profile, count, structural, semantic) in [
        ("A", 2, false, false),
        ("B", 6, true, false),
        ("C", 3, false, true),
        ("D", 7, true, true),
    ] {
        let mut client = Client::start(root.path(), profile).await;
        let listed = client.request("tools/list", json!({})).await;
        let tools = listed["result"]["tools"].as_array().unwrap();
        assert_eq!(tools.len(), count);
        assert!(
            tools
                .iter()
                .all(|t| t["inputSchema"].is_object() && t["outputSchema"].is_object())
        );
        let symbol = client.tool("find_symbol", json!({"name":"target"})).await;
        if structural {
            assert_eq!(
                symbol["structuredContent"]["results"][0]["line"], 1,
                "{symbol}"
            );
            assert_eq!(symbol["structuredContent"]["coverage"]["complete"], false);
            let callers = client.tool("find_callers", json!({"name":"target"})).await;
            assert_eq!(
                callers["structuredContent"]["results"][0]["caller"],
                "caller"
            );
            // Uniform across the page, so it is stated once on the page and the rows omit it.
            assert_eq!(
                callers["structuredContent"]["resolution"],
                "unique_name_candidate"
            );
            assert!(callers["structuredContent"]["results"][0]["resolution"].is_null());
            let trace = client
                .tool(
                    "trace_dependencies",
                    json!({"name":"target","direction":"callers","depth":2}),
                )
                .await;
            assert_eq!(
                trace["structuredContent"]["results"][1]["caller"], "outer",
                "{trace}"
            );
            assert_eq!(trace["structuredContent"]["results"][1]["depth"], 2);
            // A session is a session in which code changes. Every question reads the repository
            // as it is on disk, so a definition written a moment ago is already answerable - the
            // snapshot this used to hold would have denied it until the server restarted.
            std::fs::write(root.path().join("new.rs"), "fn added_later() {}\n").unwrap();
            let arrived = client
                .tool("find_symbol", json!({"name":"added_later"}))
                .await;
            assert_eq!(
                arrived["structuredContent"]["results"][0]["path"], "new.rs",
                "{arrived}"
            );
            std::fs::remove_file(root.path().join("new.rs")).unwrap();
            let gone = client.tool("find_symbol", json!({"name":"added_later"})).await;
            assert_eq!(gone["structuredContent"]["symbol_status"], "unknown_symbol", "{gone}");
        } else {
            assert_eq!(symbol["isError"], true);
        }
        let result = client
            .tool("search_concept", json!({"query":"target behavior"}))
            .await;
        if semantic {
            // The default lexical ranker answers without any backend or embedding service.
            assert_ne!(result["isError"], true, "{result}");
            assert_eq!(
                result["structuredContent"]["backend"], "bm25/symbol-chunks",
                "{result}"
            );
        } else {
            assert_eq!(result["isError"], true);
            let error = result["structuredContent"]["error"].as_str().unwrap();
            assert!(error.contains("disabled"));
        }
        client.stop().await;
    }
}

/// Both rankers page by the same rule. The in-process one used to answer `limit: 1000` with 100
/// rows and `offset: 99999` with an empty page, which reads as "nothing here" rather than "you
/// asked for a page that cannot exist".
#[tokio::test]
async fn concept_search_enforces_the_page_bounds_it_documents() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(root.path().join("sample.rs"), "/// Wrap text.\nfn wrap() {}\n").unwrap();
    let mut client = Client::start(root.path(), "C").await;
    for (arguments, expected) in [
        (json!({"query": "wrap text", "limit": 1000}), "limit must be 1..100"),
        (json!({"query": "wrap text", "limit": 0}), "limit must be 1..100"),
        (json!({"query": "wrap text", "offset": 99999}), "offset must be 0..10000"),
    ] {
        let result = client.tool("search_concept", arguments.clone()).await;
        assert_eq!(result["isError"], true, "{arguments} -> {result}");
        let error = result["structuredContent"]["error"].as_str().unwrap();
        assert!(error.contains(expected), "{arguments} -> {error}");
    }
    let page = client
        .tool("search_concept", json!({"query": "wrap text", "limit": 5}))
        .await;
    assert_ne!(page["isError"], true, "{page}");
    client.stop().await;
}

/// A page the schema allows must come back as a page. On Django, `find_callers("get")` with
/// `limit: 40` serialises past the response cap, and the server used to answer a legal request
/// with an error and no rows. It now returns as many rows as fit and says there are more.
#[tokio::test]
async fn an_oversized_page_is_trimmed_rather_than_refused() {
    let root = tempfile::tempdir().unwrap();
    // What pushes a legal page past the cap on a real repository is the per-row weight: each row
    // carries its own call-site excerpt and expression. Namesakes add the page-level definitions.
    for module in 0..6 {
        std::fs::write(
            root.path().join(format!("defs{module}.rs")),
            "pub fn target(value: usize) -> usize { value }\n",
        )
        .unwrap();
    }
    // Every row carries its own path, enclosing caller, excerpt and expression, so a fixture that
    // overflows needs weight in each: a deep directory, long caller names and long call lines.
    let padding = "_".repeat(200);
    let directory = root.path().join(format!("nested{padding}")).join("inner");
    std::fs::create_dir_all(&directory).unwrap();
    let calls: String = (0..200)
        .map(|nth| {
            format!(
                "fn caller{padding}{nth}() {{\n    let value{padding}{nth} = target({nth}) + {nth};\n}}\n"
            )
        })
        .collect();
    std::fs::write(directory.join("calls.rs"), calls).unwrap();
    let mut client = Client::start(root.path(), "B").await;
    let page = client
        .tool("find_callers", json!({"name": "target", "limit": 100}))
        .await;
    assert_ne!(page["isError"], true, "{page}");
    let rows = page["structuredContent"]["results"].as_array().unwrap();
    assert!(!rows.is_empty() && rows.len() < 100, "{} rows", rows.len());
    assert_eq!(page["structuredContent"]["has_more"], true);
    let next = page["structuredContent"]["next_offset"].as_u64().unwrap() as usize;
    assert_eq!(next, rows.len(), "paging resumes where the trimmed page stopped");
    let second = client
        .tool("find_callers", json!({"name": "target", "limit": 100, "offset": next}))
        .await;
    let more = second["structuredContent"]["results"].as_array().unwrap();
    let line = |row: &serde_json::Value| (row["line"].as_u64(), row["column"].as_u64());
    assert!(
        !more.is_empty() && line(&more[0]) != line(&rows[rows.len() - 1]),
        "the next page must continue past the trimmed one"
    );
    client.stop().await;
}

/// Every install path asks a binary what it is - a release script, a package manager, a user
/// checking which build their client launched - and the answer has to be the version rather than
/// `missing value for --version`, which is what the argument parser used to say.
#[test]
fn the_binary_reports_its_own_version_and_needs_no_root_to_do_it() {
    for flag in ["--version", "-V"] {
        let asked = std::process::Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"))
            .arg(flag)
            .output()
            .unwrap();
        assert!(asked.status.success(), "{flag}: {asked:?}");
        assert_eq!(
            String::from_utf8(asked.stdout).unwrap().trim(),
            format!("retrieval-mcp {}", env!("CARGO_PKG_VERSION"))
        );
    }
}

#[tokio::test]
async fn an_explicit_tool_list_gates_exactly_what_it_names() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(root.path().join("sample.rs"), "fn target() {}\n").unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"));
    command.args([
        "--root",
        root.path().to_str().unwrap(),
        "--tools",
        "read_source,inspect_symbol",
    ]);
    let mut client = Client::spawn(command).await;
    let listed = client.request("tools/list", json!({})).await;
    let names: Vec<&str> = listed["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, ["read_source", "inspect_symbol"]);
    // An unlisted tool is absent from the catalogue and refused by name, exactly like a profile.
    let refused = client.tool("find_callers", json!({"name":"target"})).await;
    assert_eq!(refused["isError"], true);
    let allowed = client.tool("inspect_symbol", json!({"name":"target"})).await;
    assert_eq!(allowed["structuredContent"]["symbol_status"], "indexed");
    client.stop().await;
}

/// A caller question is answered without building a whole-repository index: the rows are what the
/// parser finds in the files that write the name, the coverage says how much of the repository
/// that was, and no `index_built` event is logged because nothing is built.
#[tokio::test]
async fn a_session_answers_callers_without_building_an_index() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(root.path().join("util.py"), "def normalize(value):\n    return value\n")
        .unwrap();
    std::fs::write(
        root.path().join("service.py"),
        "from util import normalize\n\n\ndef handle(row):\n    return normalize(row)\n",
    )
    .unwrap();
    std::fs::write(root.path().join("other.py"), "def spin():\n    return 1\n").unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"));
    command.args([
        "--root",
        root.path().to_str().unwrap(),
        "--tools",
        "find_callers",
        "--run-id",
        "integration",
    ]);
    let mut client = Client::spawn(command).await;
    let answered = client.tool("find_callers", json!({"name": "normalize"})).await;
    let payload = &answered["structuredContent"];
    assert_eq!(payload["symbol_status"], "indexed");
    let callers = payload["results"].as_array().unwrap();
    assert_eq!(callers.len(), 1);
    assert_eq!(callers[0]["caller"], "handle");
    assert_eq!(callers[0]["path"], "service.py");
    // Two of the three source files write the name; the third was never parsed, and the coverage
    // says so rather than implying a three-file repository.
    assert_eq!(payload["coverage"]["indexed_files"], 2);
    assert_eq!(payload["coverage"]["eligible_files"], 3);
    assert_eq!(payload["coverage"]["budget_truncated"], false);
    let logs = client.stop().await;
    assert!(logged(&logs, "index_built").is_empty(), "{logs:?}");
    assert_eq!(logged(&logs, "index_mode")[0]["mode"], "per-question");
}

#[tokio::test]
async fn the_unrestricted_default_exposes_only_the_tools_that_repaid_their_schema() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(root.path().join("sample.rs"), "fn target() {}\nfn caller() { target() }\n").unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"));
    command.args(["--root", root.path().to_str().unwrap()]);
    let mut client = Client::spawn(command).await;
    let listed = client.request("tools/list", json!({})).await;
    let names: Vec<&str> = listed["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect();
    assert_eq!(
        names,
        ["search_exact", "read_source", "find_callers", "search_concept"],
        "the default surface is measured, not maximal"
    );
    // The un-defaulted tools are not deleted: naming them still works.
    let refused = client.tool("trace_dependencies", json!({"name":"target"})).await;
    assert_eq!(refused["isError"], true);
    let callers = client.tool("find_callers", json!({"name":"target"})).await;
    assert_eq!(callers["structuredContent"]["results"][0]["caller"], "caller");
    client.stop().await;
}

/// A request the server accepted is a reply it owes, even if the client closes stdin immediately.
///
/// Pipelines, scripts and tests all write their requests and close the pipe. rmcp treats end of
/// input as a shutdown signal and drains in-flight work for five seconds before giving up, so any
/// handler slower than that - a first structural call over a large repository takes eighteen
/// seconds - finishes internally while its response is discarded, and the request appears to
/// vanish. `DrainingStdin` in `main.rs` withholds end-of-input instead. The fixture backend sleeps
/// past that window, which is what makes this test fail without the fix.
#[cfg(unix)]
#[tokio::test]
async fn a_reply_is_flushed_even_when_stdin_closes_during_a_slow_call() {
    use std::io::Write;
    use std::process::Stdio;

    let root = tempfile::tempdir().unwrap();
    std::fs::write(root.path().join("sample.rs"), "fn meaning() {}\n").unwrap();
    let payload = json!({"protocol_version":1,"backend":"fixture","index_note":"test only","has_more":false,
        "results":[{"path":"sample.rs","start_line":1,"end_line":1,"score":0.75}]});
    let backend = json!([
        "/bin/sh",
        "-c",
        format!("read -r request || true; sleep 6; printf '%s' '{payload}'")
    ]);

    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"))
        .args([
            "--root",
            root.path().to_str().unwrap(),
            "--ranker",
            "semantic",
            "--semantic-command",
            &backend.to_string(),
            "--timeout-seconds",
            "30",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();

    // Write both requests, then close the pipe without waiting for either answer.
    let mut stdin = child.stdin.take().unwrap();
    for line in [
        json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}),
        json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search_concept","arguments":{"query":"describe behavior","limit":1}}}),
    ] {
        writeln!(stdin, "{line}").unwrap();
    }
    stdin.flush().unwrap();
    drop(stdin);

    let finished = child.wait_with_output().unwrap();
    let answered: Vec<Value> = String::from_utf8(finished.stdout)
        .unwrap()
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    let call = answered
        .iter()
        .find(|message| message["id"] == 2)
        .unwrap_or_else(|| panic!("the accepted tools/call was never answered: {answered:?}"));
    assert_ne!(call["result"]["isError"], true, "{call}");
    assert_eq!(
        call["result"]["structuredContent"]["results"][0]["symbol"]["symbol"],
        "sample.rs::meaning"
    );
}

/// A corpus the test writes itself, sized so the first structural call spends real time building
/// its snapshot: 200 files take about 120 ms in a debug build here, twice the 50 ms dispatch grace
/// in `main.rs` and well short of a second. Every symbol name carries the file it lives in, so a
/// reply that belongs to a different request is visible in the payload and not only in its id.
fn generated_corpus(files: usize) -> tempfile::TempDir {
    let root = tempfile::tempdir().unwrap();
    for file in 0..files {
        let mut text = String::new();
        for symbol in 0..10 {
            text.push_str(&format!(
                "fn helper_{file}_{symbol}(value: usize) -> usize {{ let mut total = value; \
                 for step in 0..{symbol} {{ total += step; }} total }}\n"
            ));
            text.push_str(&format!(
                "fn caller_{file}_{symbol}() -> usize {{ helper_{file}_{symbol}({symbol}) }}\n"
            ));
        }
        std::fs::write(root.path().join(format!("mod_{file}.rs")), text).unwrap();
    }
    root
}

/// Every measurement this project has made drove one request at a time. A client that writes its
/// requests without waiting - a pipelined session, or an agent firing two tools at once - must get
/// all of them back, each carrying the id that asked for it. Handlers finishing at the same moment
/// share one stdout: interleaved writes would corrupt the framing, and a misrouted reply would
/// answer one caller with another's rows. `collect` refuses unknown and repeated ids, so neither
/// can pass as a skipped line.
#[tokio::test]
async fn pipelined_calls_are_each_answered_with_the_id_that_asked() {
    let root = generated_corpus(200);
    let mut client = Client::start(root.path(), "D").await;
    let mut exact = Vec::new();
    let mut source = Vec::new();
    let mut symbol = Vec::new();
    // Written back-to-back with nothing awaited: the server holds all twenty-four at once, and the
    // structural third of them queues behind one lazy index build.
    for file in 0..8 {
        exact.push(
            client
                .dispatch_tool(
                    "search_exact",
                    json!({"query": format!("caller_{file}_3"), "limit": 5}),
                )
                .await,
        );
        source.push(
            client
                .dispatch_tool(
                    "read_source",
                    json!({"path": format!("mod_{file}.rs"), "start_line": 1, "end_line": 2}),
                )
                .await,
        );
        symbol.push(
            client
                .dispatch_tool("find_symbol", json!({"name": format!("helper_{file}_7")}))
                .await,
        );
    }
    let owed: Vec<u64> = exact
        .iter()
        .chain(&source)
        .chain(&symbol)
        .copied()
        .collect();
    let answered = client.collect(&owed).await;
    for file in 0..8usize {
        let found = &answered[&exact[file]]["result"]["structuredContent"];
        let rows = found["results"].as_array().unwrap();
        assert!(!rows.is_empty(), "{found}");
        for row in rows {
            assert_eq!(row["path"], format!("mod_{file}.rs"), "{found}");
            assert!(
                row["snippet"]
                    .as_str()
                    .unwrap()
                    .contains(&format!("caller_{file}_3")),
                "{found}"
            );
        }
        let read = &answered[&source[file]]["result"]["structuredContent"];
        assert_eq!(read["path"], format!("mod_{file}.rs"), "{read}");
        assert!(
            read["lines"][0]["text"]
                .as_str()
                .unwrap()
                .starts_with(&format!("fn helper_{file}_0(")),
            "{read}"
        );
        let defined = &answered[&symbol[file]]["result"]["structuredContent"];
        assert_eq!(defined["symbol_status"], "indexed", "{defined}");
        assert_eq!(
            defined["results"][0]["name"],
            format!("helper_{file}_7"),
            "{defined}"
        );
        assert_eq!(defined["results"][0]["path"], format!("mod_{file}.rs"));
    }
    client.stop().await;
}

/// Eight structural calls issued at once. There is no shared index to race any more - each answer
/// searches the repository for its own question - so what has to hold is that all eight are
/// answered, each about the corpus it was asked about, and each saying how much of that corpus it
/// read. A per-question scan that reported a different repository size per call, or dropped a
/// reply under concurrency, would fail here.
#[tokio::test]
async fn concurrent_structural_calls_are_each_answered_over_one_corpus() {
    let root = generated_corpus(200);
    let mut client = Client::start(root.path(), "D").await;
    let mut owed = Vec::new();
    for file in 0..2 {
        let name = format!("helper_{file}_4");
        owed.push(
            client
                .dispatch_tool("find_symbol", json!({ "name": name }))
                .await,
        );
        owed.push(
            client
                .dispatch_tool("find_callers", json!({ "name": name }))
                .await,
        );
        owed.push(
            client
                .dispatch_tool("inspect_symbol", json!({ "name": name }))
                .await,
        );
        owed.push(
            client
                .dispatch_tool(
                    "trace_dependencies",
                    json!({"name": name, "direction": "callers", "depth": 2}),
                )
                .await,
        );
    }
    let answered = client.collect(&owed).await;
    for id in &owed {
        let content = &answered[id]["result"]["structuredContent"];
        assert_ne!(answered[id]["result"]["isError"], true, "{content}");
        let coverage = &content["coverage"];
        // Each answer parsed the files its own question needed, out of one repository.
        assert_eq!(coverage["eligible_files"], 200, "{coverage}");
        assert!(coverage["indexed_files"].as_u64().unwrap() <= 200, "{coverage}");
        assert_eq!(coverage["budget_truncated"], false, "{coverage}");
    }
    // Answering many questions at once is not an excuse for answering the wrong one.
    for (file, batch) in owed.chunks(4).enumerate() {
        let rows = |id: &u64| answered[id]["result"]["structuredContent"]["results"].clone();
        assert_eq!(rows(&batch[0])[0]["name"], format!("helper_{file}_4"));
        assert_eq!(rows(&batch[1])[0]["caller"], format!("caller_{file}_4"));
        assert_eq!(
            rows(&batch[2])[0]["symbol"],
            format!("mod_{file}.rs::helper_{file}_4")
        );
        assert_eq!(rows(&batch[3])[0]["callee"], format!("helper_{file}_4"));
    }
    let logs = client.stop().await;
    // Nothing is built ahead of a question, so nothing logs a build.
    assert!(logged(&logs, "index_built").is_empty(), "{logs:?}");
}

/// A client that gives up on a call must not take the session with it. The abandoned request is the
/// first structural one, so it is cancelled while its index build is running - the expensive case -
/// and two stale cancellations follow: one for a call already answered, one for an id that was
/// never issued. MCP forbids answering a cancelled request, and `collect` fails on any reply it was
/// not told to expect, so a resurrected answer for the cancelled id fails here too. `stop` allows
/// the process five seconds to exit, so a cancelled call that leaked its in-flight count - holding
/// `DrainingStdin` open forever - fails there.
#[tokio::test]
async fn a_cancelled_request_does_not_wedge_the_calls_behind_it() {
    let root = generated_corpus(200);
    let mut client = Client::start(root.path(), "D").await;
    let settled = client
        .request(
            "tools/call",
            json!({"name":"search_exact","arguments":{"query":"helper_1_1","limit":1}}),
        )
        .await;
    assert_ne!(settled["result"]["isError"], true, "{settled}");
    let settled_id = settled["id"].as_u64().unwrap();

    let abandoned = client
        .dispatch_tool("find_symbol", json!({"name":"helper_2_2"}))
        .await;
    for (request, reason) in [
        (abandoned, "client gave up"),
        (settled_id, "stale: already answered"),
        (9_999, "never issued"),
    ] {
        client
            .send(json!({"jsonrpc":"2.0","method":"notifications/cancelled",
                         "params":{"requestId":request,"reason":reason}}))
            .await;
    }

    let after = [
        client
            .dispatch_tool("find_symbol", json!({"name":"helper_3_3"}))
            .await,
        client
            .dispatch_tool("find_callers", json!({"name":"helper_3_3"}))
            .await,
        client
            .dispatch_tool("search_exact", json!({"query":"caller_4_4","limit":2}))
            .await,
    ];
    let answered = client.collect(&after).await;
    let content = |id: &u64| answered[id]["result"]["structuredContent"].clone();
    assert_eq!(
        content(&after[0])["results"][0]["name"],
        "helper_3_3",
        "{}",
        content(&after[0])
    );
    assert_eq!(content(&after[1])["results"][0]["caller"], "caller_3_3");
    assert_eq!(content(&after[2])["results"][0]["path"], "mod_4.rs");

    let logs = client.stop().await;
    // The abandoned call is answered in milliseconds now that nothing waits on an index build, so
    // whether the cancellation lands before, during or after it is a race this test cannot pin.
    // What it does pin is that the session survived all three notifications - one for a call in
    // flight, one already answered, one never issued - and `collect` above fails on any reply the
    // client was not owed, including a resurrected one for the abandoned id.
    assert!(
        logs.iter()
            .any(|line| line["event"] == "tool_end" && line["request_id"] == abandoned),
        "the cancelled call left no record at all: {logs:?}"
    );
}

/// The drain in `DrainingStdin` was measured once, against a sleeping backend and a single call.
/// Under a pipeline it has more to hold: nine requests arrive, stdin ends immediately, and the six
/// structural ones wait on an index build that outlasts the 50 ms dispatch grace. Every accepted
/// request is still a reply owed, and one snapshot must serve them all.
#[tokio::test]
async fn every_reply_survives_stdin_closing_mid_index_build() {
    use std::io::Write;

    let root = generated_corpus(200);
    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"))
        .args([
            "--root",
            root.path().to_str().unwrap(),
            "--profile",
            "D",
            "--timeout-seconds",
            "60",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();

    let mut written = vec![
        json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}),
        json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
    ];
    for file in 0..6u64 {
        written.push(json!({"jsonrpc":"2.0","id":10 + file,"method":"tools/call",
            "params":{"name":"find_symbol","arguments":{"name": format!("helper_{file}_5")}}}));
    }
    for file in 0..3u64 {
        written.push(json!({"jsonrpc":"2.0","id":20 + file,"method":"tools/call",
            "params":{"name":"search_exact","arguments":{"query": format!("caller_{file}_6"),"limit":2}}}));
    }
    let mut stdin = child.stdin.take().unwrap();
    for line in &written {
        writeln!(stdin, "{line}").unwrap();
    }
    stdin.flush().unwrap();
    drop(stdin);

    let finished = child.wait_with_output().unwrap();
    assert!(finished.status.success(), "{:?}", finished.status);
    let replies: Vec<Value> = String::from_utf8(finished.stdout)
        .unwrap()
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    let answer = |id: u64| {
        replies
            .iter()
            .find(|reply| reply["id"] == id)
            .unwrap_or_else(|| panic!("accepted request {id} was never answered: {replies:?}"))
    };
    for file in 0..6u64 {
        let content = &answer(10 + file)["result"]["structuredContent"];
        assert_eq!(content["results"][0]["name"], format!("helper_{file}_5"));
        // Every answer describes the same repository, however the shutdown interleaved them.
        assert_eq!(content["coverage"]["eligible_files"], 200, "{content}");
    }
    for file in 0..3u64 {
        let content = &answer(20 + file)["result"]["structuredContent"];
        assert_eq!(content["results"][0]["path"], format!("mod_{file}.rs"), "{content}");
    }
    let built = String::from_utf8(finished.stderr)
        .unwrap()
        .lines()
        .filter(|line| line.contains(r#""event":"index_built""#))
        .count();
    assert_eq!(built, 0, "nothing is indexed ahead of a question, yet {built} builds were logged");
}

#[cfg(unix)]
#[tokio::test]
async fn semantic_subprocess_contract_over_stdio() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(root.path().join("sample.rs"), "fn meaning() {}\n").unwrap();
    let payload = json!({"protocol_version":1,"backend":"fixture","index_note":"test only","has_more":false,
        "results":[{"path":"sample.rs","start_line":1,"end_line":1,"score":0.75}]});
    let command = json!([
        "/bin/sh",
        "-c",
        format!("read -r request || true; printf '%s' '{payload}'")
    ]);
    let mut client = Client::with_backend(root.path(), "D", Some(command)).await;
    let lean = client
        .tool(
            "search_concept",
            json!({"query":"describe behavior","limit":1}),
        )
        .await;
    assert_ne!(lean["isError"], true, "{lean}");
    let row = &lean["structuredContent"]["results"][0];
    // A row names the enclosing definition and separates name-scoped caller candidates from call
    // expressions syntactically owned by that exact definition; source stays out by default.
    assert_eq!(row["symbol"]["symbol"], "sample.rs::meaning", "{lean}");
    assert_eq!(row["symbol"]["kind"], "function_item");
    assert_eq!(row["symbol"]["name_candidate_callers"], 0);
    assert_eq!(row["symbol"]["direct_callees"], 0);
    assert!(row["excerpt"].is_null(), "{lean}");
    let full = client
        .tool(
            "search_concept",
            json!({"query":"describe behavior","limit":1,"fields":["excerpt"]}),
        )
        .await;
    assert_eq!(
        full["structuredContent"]["results"][0]["excerpt"],
        "fn meaning() {}"
    );
    let logs = client.stop().await;
    let end = logs.iter().find(|l| l["event"] == "tool_end").unwrap();
    assert_eq!(end["backend"], "fixture");
    assert_eq!(end["arguments"]["query"], "describe behavior");
}

#[tokio::test]
#[ignore = "requires a running local Ollama with nomic-embed-text and a built ollama_backend example"]
async fn real_ollama_semantic_over_stdio() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(root.path().join("retry.rs"), "/// Retry failed network requests with exponential backoff.\nfn retry_delay(attempt: u32) -> u64 { 2_u64.pow(attempt.min(8)) }\n").unwrap();
    std::fs::write(root.path().join("color.rs"), "/// Convert RGB color channels to a hexadecimal display string.\nfn color_hex(r: u8, g: u8, b: u8) -> String { format!(\"#{r:02x}{g:02x}{b:02x}\") }\n").unwrap();
    let backend = std::path::Path::new(env!("CARGO_BIN_EXE_retrieval-mcp"))
        .parent()
        .unwrap()
        .join("examples/ollama_backend");
    let mut client = Client::with_backend(root.path(), "D", Some(json!([backend]))).await;
    let result = client.tool("search_concept", json!({"query":"waiting longer between repeated attempts after a network failure","limit":1})).await;
    assert_ne!(result["isError"], true, "{result}");
    assert_eq!(
        result["structuredContent"]["results"][0]["path"],
        "retry.rs"
    );
    assert!(
        result["structuredContent"]["results"][0]["score"]
            .as_f64()
            .unwrap()
            .is_finite()
    );
    let repeated = client
        .tool(
            "search_concept",
            json!({"query":"waiting longer between attempts","limit":1}),
        )
        .await;
    assert_ne!(repeated["isError"], true, "{repeated}");
    assert!(
        repeated["structuredContent"]["index_note"]
            .as_str()
            .unwrap()
            .contains("2 document embeddings reused, 0 embedded")
    );
    std::fs::remove_file(root.path().join("color.rs")).unwrap();
    let refreshed = client
        .tool(
            "search_concept",
            json!({"query":"waiting longer between attempts","limit":1}),
        )
        .await;
    assert_ne!(refreshed["isError"], true, "{refreshed}");
    assert!(
        refreshed["structuredContent"]["index_note"]
            .as_str()
            .unwrap()
            .contains("1 document embeddings reused, 0 embedded")
    );
    client.stop().await;
}

/// A stdio server is launched inside the project the user opened, and the client already knows
/// that directory, so a per-project entry repeating it as `--root .` is a copy that can go stale.
/// The root arrives percent-escaped, so a directory with a space in its name is the case that
/// proves it is decoded rather than handed to the filesystem as written.
#[tokio::test]
async fn a_session_without_a_root_flag_reads_the_repository_its_client_reports() {
    let parent = tempfile::tempdir().unwrap();
    let root = parent.path().join("My Projects");
    std::fs::create_dir(&root).unwrap();
    std::fs::write(
        root.join("sample.rs"),
        "fn target() {}\nfn caller() { target(); }\n",
    )
    .unwrap();
    let mut client = Client::start_with_roots(&[&root], "B").await;
    let callers = client.tool("find_callers", json!({"name":"target"})).await;
    assert_eq!(
        callers["structuredContent"]["results"][0]["caller"], "caller",
        "{callers}"
    );
    assert_eq!(
        callers["structuredContent"]["results"][0]["path"], "sample.rs",
        "{callers}"
    );
    assert_eq!(client.asked, 1, "the root is asked for once, then remembered");
    let logs = client.stop().await;
    let adopted = logged(&logs, "root_adopted");
    assert_eq!(adopted.len(), 1, "{logs:?}");
    assert!(
        adopted[0]["root"].as_str().unwrap().ends_with("My Projects"),
        "{:?}",
        adopted[0]
    );
}

/// The notification only says the list changed, which usually leaves this server's root alone:
/// Claude Code sends one whenever a working directory is added, and the project stays first in
/// the list. So an unmoved root must not send the server back to the client to re-resolve, while
/// a moved one must be followed - and then the old repository is no longer readable at all.
#[tokio::test]
async fn a_moved_client_root_is_followed_and_an_unmoved_one_is_left_alone() {
    let first = tempfile::tempdir().unwrap();
    std::fs::write(
        first.path().join("first.rs"),
        "fn alpha() {}\nfn caller() { alpha(); }\n",
    )
    .unwrap();
    let second = tempfile::tempdir().unwrap();
    std::fs::write(
        second.path().join("second.rs"),
        "fn beta() {}\nfn caller() { beta(); }\n",
    )
    .unwrap();
    let mut client = Client::start_with_roots(&[first.path()], "B").await;
    let before = client.tool("find_symbol", json!({"name":"alpha"})).await;
    assert_eq!(
        before["structuredContent"]["results"][0]["path"], "first.rs",
        "{before}"
    );

    client.move_roots(&[first.path()]).await;
    client.await_event("roots_changed", 1).await;
    let unmoved = client.tool("find_symbol", json!({"name":"alpha"})).await;
    assert_eq!(
        unmoved["structuredContent"]["results"][0]["path"], "first.rs",
        "{unmoved}"
    );

    client.move_roots(&[second.path()]).await;
    client.await_event("roots_changed", 2).await;
    let moved = client.tool("find_symbol", json!({"name":"beta"})).await;
    assert_eq!(
        moved["structuredContent"]["results"][0]["path"], "second.rs",
        "{moved}"
    );
    // Following a root means the old one stops answering, which the next assertion proves.
    let gone = client.tool("find_symbol", json!({"name":"alpha"})).await;
    assert!(
        gone["structuredContent"]["results"]
            .as_array()
            .unwrap()
            .is_empty(),
        "the old root is no longer readable: {gone}"
    );
    let logs = client.stop().await;
    assert_eq!(logged(&logs, "root_changed").len(), 1, "{logs:?}");
}

/// A client that reports no roots has still told the server where it is: every stdio MCP server is
/// launched in the project the user opened, which is the same directory `--root .` resolved
/// against. Codex 0.154.0 declares no roots capability at all and is the reason this path exists;
/// a client that declares the capability but reports an empty list lands in the same place.
#[tokio::test]
async fn a_client_that_reports_no_roots_reads_the_directory_it_launched_the_server_in() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(
        root.path().join("sample.rs"),
        "fn target() {}\nfn caller() { target(); }\n",
    )
    .unwrap();

    let mut command = Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"));
    command.args(["--profile", "B"]).current_dir(root.path());
    let mut declaring = Client::spawn_declaring(command, Some(Vec::new())).await;
    let answered = declaring.tool("find_symbol", json!({"name":"target"})).await;
    assert_eq!(
        answered["structuredContent"]["results"][0]["path"], "sample.rs",
        "{answered}"
    );
    assert_eq!(declaring.asked, 1, "a declared capability is still asked");
    let logs = declaring.stop().await;
    assert_eq!(logged(&logs, "roots_empty").len(), 1, "{logs:?}");
    assert_eq!(logged(&logs, "launch_directory").len(), 1, "{logs:?}");

    // The Codex shape: no roots capability in the handshake, so nothing is asked at all.
    let mut command = Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"));
    command.args(["--profile", "B"]).current_dir(root.path());
    let mut silent = Client::spawn(command).await;
    let without_asking = silent.tool("find_symbol", json!({"name":"target"})).await;
    assert_eq!(
        without_asking["structuredContent"]["results"][0]["path"], "sample.rs",
        "{without_asking}"
    );
    assert_eq!(silent.asked, 0, "a client without the capability is not asked");
    silent.stop().await;
}

/// A client may declare roots and then never answer. Without a deadline the first tool call never
/// returns and every later one queues behind it, so a silence is read as the absence it looks like
/// and the launch directory answers instead.
#[tokio::test]
async fn a_client_that_declares_roots_and_never_answers_falls_back_rather_than_hanging() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(
        root.path().join("sample.rs"),
        "fn target() {}\nfn caller() { target(); }\n",
    )
    .unwrap();

    let mut command = Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"));
    command
        .args(["--profile", "B", "--timeout-seconds", "1"])
        .current_dir(root.path());
    let mut client = Client::spawn_mute(command).await;

    let answered = tokio::time::timeout(
        Duration::from_secs(20),
        client.tool("find_symbol", json!({"name":"target"})),
    )
    .await
    .expect("a mute client must not hang the session");
    assert_eq!(
        answered["structuredContent"]["results"][0]["path"], "sample.rs",
        "{answered}"
    );
    assert_eq!(client.asked, 1, "the declared capability is asked once");

    let logs = client.stop().await;
    assert_eq!(logged(&logs, "roots_timed_out").len(), 1, "{logs:?}");
    assert_eq!(logged(&logs, "launch_directory").len(), 1, "{logs:?}");
}

/// Instructions are paid for in the prompt prefix of every turn, and a default install refuses
/// three of the seven tools. Routing the model to a tool it cannot call is advice whose only
/// possible outcome is an error.
#[tokio::test]
async fn handshake_instructions_name_only_the_tools_this_session_exposes() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(root.path().join("sample.rs"), "fn target() {}\n").unwrap();
    let absent = ["find_symbol", "inspect_symbol", "trace_dependencies"];

    let mut command = Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"));
    command.arg("--root").arg(root.path());
    let client = Client::spawn(command).await;
    let default = client.instructions.clone();
    client.stop().await;
    for tool in absent {
        assert!(!default.contains(tool), "default surface routes to {tool}: {default}");
    }
    for tool in ["search_exact", "read_source", "find_callers", "search_concept"] {
        assert!(default.contains(tool), "default surface omits {tool}: {default}");
    }

    let mut command = Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"));
    command.arg("--root").arg(root.path()).args(["--profile", "D"]);
    let client = Client::spawn(command).await;
    let everything = client.instructions.clone();
    client.stop().await;
    for tool in absent {
        assert!(everything.contains(tool), "profile D omits {tool}: {everything}");
    }
    assert!(default.len() < everything.len(), "a smaller surface is cheaper to describe");
}

/// Two launch directories are corpora of everything rather than repositories, and indexing them
/// takes minutes to answer about files nobody asked about. An operator who means it says `--root`.
#[tokio::test]
async fn a_launch_directory_that_cannot_be_a_repository_is_refused_by_name() {
    let home = tempfile::tempdir().unwrap();
    for (directory, home_variable, named) in [
        (home.path(), Some(home.path()), "your home directory"),
        (std::path::Path::new("/"), None, "the filesystem root"),
    ] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"));
        command.args(["--profile", "B"]).current_dir(directory);
        if let Some(home_variable) = home_variable {
            command.env("HOME", home_variable);
        }
        let mut client = Client::spawn(command).await;
        let refused = client.tool("find_symbol", json!({"name":"target"})).await;
        assert_eq!(refused["isError"], true, "{named}: {refused}");
        let reported = refused["structuredContent"]["error"].as_str().unwrap();
        assert!(
            reported.contains(named) && reported.contains("--root PATH"),
            "{reported}"
        );
        client.stop().await;
    }
}

/// `--root` is how an operator points a session at a directory the client did not open, so a
/// pinned root is never asked about and never moved by a notification.
#[tokio::test]
async fn an_explicit_root_is_neither_asked_about_nor_overridden() {
    let pinned = tempfile::tempdir().unwrap();
    std::fs::write(pinned.path().join("pinned.rs"), "fn alpha() {}\n").unwrap();
    let reported = tempfile::tempdir().unwrap();
    std::fs::write(reported.path().join("reported.rs"), "fn beta() {}\n").unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_retrieval-mcp"));
    command.args([
        "--root",
        pinned.path().to_str().unwrap(),
        "--profile",
        "B",
    ]);
    let mut client =
        Client::spawn_declaring(command, Some(vec![file_uri(reported.path())])).await;
    let pinned_hit = client.tool("find_symbol", json!({"name":"alpha"})).await;
    assert_eq!(
        pinned_hit["structuredContent"]["results"][0]["path"], "pinned.rs",
        "{pinned_hit}"
    );
    client.move_roots(&[reported.path()]).await;
    client.await_event("roots_changed_ignored", 1).await;
    let still_pinned = client.tool("find_symbol", json!({"name":"beta"})).await;
    assert!(
        still_pinned["structuredContent"]["results"]
            .as_array()
            .unwrap()
            .is_empty(),
        "the client's root must not replace --root: {still_pinned}"
    );
    assert_eq!(client.asked, 0, "a pinned session never asks for roots");
    client.stop().await;
}

/// The CLI transport arm must be the same retriever, not a second one that shares a name.
///
/// `runs/cli-transport-20260924` compares MCP against a command over one index, and its whole
/// claim to be a one-variable comparison is that only the channel differs. Both routes go through
/// `RetrievalServer::execute`, so this asserts the property that keeps them honest: for the same
/// query, `retrieval --json` emits exactly the payload the MCP tool call returns. If a renderer,
/// an argument name or a default ever drifts, the run stops being about transport.
#[tokio::test]
async fn the_cli_and_the_mcp_tool_return_the_same_payload() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(
        root.path().join("sample.rs"),
        "fn needle() {}\nfn caller() { needle(); }\nfn outer() { caller(); }\n",
    )
    .unwrap();

    let cases: [(&str, Value, &[&str]); 3] = [
        (
            "search_exact",
            json!({"query":"needle","limit":5}),
            &["search", "needle", "--limit", "5"],
        ),
        (
            "read_source",
            json!({"path":"sample.rs","start_line":2,"end_line":2}),
            &["read", "sample.rs", "--start-line", "2", "--end-line", "2"],
        ),
        (
            "find_callers",
            json!({"name":"caller","limit":5}),
            &["callers", "caller", "--limit", "5"],
        ),
    ];

    let mut client = Client::start(root.path(), "D").await;
    for (tool, args, argv) in cases {
        let over_mcp = client.tool(tool, args.clone()).await;
        assert_ne!(over_mcp["isError"], true, "{tool} failed over MCP: {over_mcp}");

        let run = std::process::Command::new(env!("CARGO_BIN_EXE_retrieval"))
            .args(["--root", root.path().to_str().unwrap(), "--json"])
            .args(argv)
            .output()
            .unwrap();
        assert!(
            run.status.success(),
            "{tool} failed over the CLI: {}",
            String::from_utf8_lossy(&run.stderr)
        );
        let over_cli: Value = serde_json::from_slice(&run.stdout).unwrap();

        // `coverage` carries a wall-clock snapshot id and timestamp, which differ between two
        // index builds of the same tree and say nothing about the retriever.
        let mut expected = over_mcp["structuredContent"].clone();
        let mut actual = over_cli;
        for payload in [&mut expected, &mut actual] {
            if let Some(coverage) = payload.get_mut("coverage").and_then(Value::as_object_mut) {
                coverage.remove("indexed_at_ms");
                coverage.remove("snapshot_id");
            }
        }
        assert_eq!(actual, expected, "{tool} differs between channels");
    }
    client.stop().await;
}

/// `retrieval callers X | head` must show callers, not coverage prose.
///
/// The first renderer printed every scalar field to stdout before the rows. A `find_callers`
/// payload carries about thirty of them, several being multi-sentence `coverage.*` text, so the
/// most natural pipe an agent writes returned no rows at all. The arm that change belongs to
/// measures whether a pipeable surface costs fewer round trips; shipping this would have
/// measured a broken renderer and called it a fact about transport.
#[test]
fn rows_go_to_stdout_and_coverage_prose_stays_out_of_the_pipe() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(
        root.path().join("sample.rs"),
        "fn needle() {}\nfn caller() { needle(); }\nfn outer() { caller(); }\n",
    )
    .unwrap();

    let run = std::process::Command::new(env!("CARGO_BIN_EXE_retrieval"))
        .args(["--root", root.path().to_str().unwrap(), "callers", "caller"])
        .output()
        .unwrap();
    assert!(run.status.success(), "{}", String::from_utf8_lossy(&run.stderr));
    let stdout = String::from_utf8(run.stdout).unwrap();
    let stderr = String::from_utf8(run.stderr).unwrap();

    // Nothing is hidden: the coverage fields are still emitted, on the diagnostic stream.
    assert!(stderr.contains("# coverage."), "coverage must still be reported: {stderr}");
    assert!(
        !stdout.contains("# coverage."),
        "coverage prose must not sit in the pipe: {stdout}"
    );

    // The first thing `| head` would show must be data, not preamble.
    let first_data = stdout
        .lines()
        .find(|line| !line.starts_with('#') && !line.trim().is_empty())
        .expect("stdout carries at least one row");
    assert!(
        first_data.contains("sample.rs"),
        "the first non-header line should be a caller row: {first_data}"
    );
    assert!(
        stdout.lines().take(5).any(|line| line.contains("sample.rs")),
        "a caller must appear within the first five lines, or `| head` is useless: {stdout}"
    );
}
