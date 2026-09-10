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
        ("B", 5, true, false),
        ("C", 3, false, true),
        ("D", 6, true, true),
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
