use serde_json::{Value, json};
use std::{collections::HashMap, process::Stdio, time::Duration};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
};

struct Client {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    logs: tokio::task::JoinHandle<String>,
    id: u64,
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
    async fn spawn(mut command: Command) -> Self {
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        let mut stderr = child.stderr.take().unwrap();
        let logs = tokio::spawn(async move {
            let mut text = String::new();
            stderr.read_to_string(&mut text).await.unwrap();
            text
        });
        let mut client = Self {
            stdin: child.stdin.take().unwrap(),
            stdout: BufReader::new(child.stdout.take().unwrap()),
            child,
            logs,
            id: 0,
        };
        let init = client.request("initialize", json!({"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1"}})).await;
        assert!(init.get("result").is_some(), "{init}");
        client
            .send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}))
            .await;
        client
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
                if value["id"] == self.id {
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
        tokio::time::timeout(Duration::from_secs(5), self.child.wait())
            .await
            .unwrap()
            .unwrap();
        self.logs
            .await
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
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
            assert_eq!(
                callers["structuredContent"]["results"][0]["resolution"],
                "unique_name_candidate"
            );
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
            // Full snapshots stay stable until restart; direct reads remain current.
            std::fs::write(root.path().join("new.rs"), "fn added_later() {}\n").unwrap();
            let missing = client
                .tool("find_symbol", json!({"name":"added_later"}))
                .await;
            assert!(
                missing["structuredContent"]["results"]
                    .as_array()
                    .unwrap()
                    .is_empty()
            );
            std::fs::remove_file(root.path().join("new.rs")).unwrap();
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

/// The structural snapshot is built lazily inside a `OnceCell` on the first structural call, and
/// until now only one call ever reached it. Eight issued at once race that build: all eight must be
/// answered from one snapshot rather than one snapshot each, since a per-caller rebuild would
/// multiply the cost of the whole session and let two callers reason over different corpora.
/// `snapshot_id` is stamped when a build starts, so two builds cannot share one, and the
/// `index_built` log line counts builds from outside the process.
#[tokio::test]
async fn concurrent_structural_calls_all_see_one_snapshot() {
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
    let mut snapshots = std::collections::BTreeSet::new();
    for id in &owed {
        let content = &answered[id]["result"]["structuredContent"];
        assert_ne!(answered[id]["result"]["isError"], true, "{content}");
        let coverage = &content["coverage"];
        assert_eq!(coverage["indexed_files"], 200, "{coverage}");
        assert_eq!(coverage["eligible_files"], 200, "{coverage}");
        assert_eq!(coverage["budget_truncated"], false, "{coverage}");
        snapshots.insert(coverage["snapshot_id"].as_str().unwrap().to_string());
    }
    assert_eq!(
        snapshots.len(),
        1,
        "calls racing the lazy build saw {snapshots:?}"
    );
    // Racing the build is not an excuse for answering the wrong question.
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
    let builds = logs
        .iter()
        .filter(|line| line["fields"]["event"] == "index_built")
        .count();
    assert_eq!(builds, 1, "the snapshot was built {builds} times");
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
    let record = logs
        .iter()
        .find(|line| line["event"] == "tool_end" && line["request_id"] == abandoned)
        .unwrap_or_else(|| panic!("the cancelled call left no record: {logs:?}"));
    // Proves the cancellation reached the handler rather than the test racing past it.
    assert!(
        record["error"].as_str().unwrap_or_default().contains("cancelled"),
        "{record}"
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
    let mut snapshots = std::collections::BTreeSet::new();
    for file in 0..6u64 {
        let content = &answer(10 + file)["result"]["structuredContent"];
        assert_eq!(content["results"][0]["name"], format!("helper_{file}_5"));
        snapshots.insert(content["coverage"]["snapshot_id"].as_str().unwrap().to_string());
    }
    assert_eq!(snapshots.len(), 1, "shutdown split the snapshot: {snapshots:?}");
    for file in 0..3u64 {
        let content = &answer(20 + file)["result"]["structuredContent"];
        assert_eq!(content["results"][0]["path"], format!("mod_{file}.rs"), "{content}");
    }
    let builds = String::from_utf8(finished.stderr)
        .unwrap()
        .lines()
        .filter(|line| line.contains(r#""event":"index_built""#))
        .count();
    assert_eq!(builds, 1, "the snapshot was built {builds} times");
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
