use serde_json::{Value, json};
use std::{process::Stdio, time::Duration};
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
    // Namesakes make every row carry several candidate definitions, which is what pushes a
    // legal page past the cap on a real repository.
    for module in 0..6 {
        std::fs::write(
            root.path().join(format!("defs{module}.rs")),
            "pub fn target(value: usize) -> usize { value }\n",
        )
        .unwrap();
    }
    let calls: String = (0..200)
        .map(|nth| format!("    let _{nth} = target({nth});\n"))
        .collect();
    std::fs::write(
        root.path().join("calls.rs"),
        format!("fn caller() {{\n{calls}}}\n"),
    )
    .unwrap();
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
    // A row names the enclosing definition and carries its degrees; source stays out by default.
    assert_eq!(row["symbol"]["symbol"], "sample.rs::meaning", "{lean}");
    assert_eq!(row["symbol"]["kind"], "function_item");
    assert_eq!(row["symbol"]["callers"], 0);
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
