# retrieval-mcp

A small Rust MCP server for testing whether a coding model can choose its own retrieval mechanism. It exposes five separate tools over stdio. There is no LLM router, combined search tool, or automatic fallback between methods.

```text
Claude Code / Codex
        │ MCP over stdio
        ▼
Rust retrieval server ── invocation events → JSONL
        ├── search_exact    → ripgrep subprocess
        ├── read_source     → bounded filesystem reads
        ├── find_symbol     → Tree-sitter snapshot (Rust/Python)
        ├── find_callers    → syntactic references + candidate definitions
        └── search_semantic → configurable JSON subprocess
                                   └── example: local Ollama embeddings
```

## Build and run

Requires Rust 1.90+ (tested with 1.96), a C compiler for Tree-sitter, and `rg` on `PATH`. The optional Ollama adapter also requires `curl` and a running Ollama service with an embedding model.

```sh
cd /path/to/retrieval-mcp
cargo build --locked --release --bin retrieval-mcp --example ollama_backend
cargo test --locked --all-targets
cargo clippy --locked --all-targets -- -D warnings

# Baseline: exactly two tools, no structural indexing or semantic backend.
./target/release/retrieval-mcp --root /absolute/path/to/repo --profile A

# All five tools, with the example semantic backend.
./target/release/retrieval-mcp \
  --root /absolute/path/to/repo --profile D \
  --semantic-command '["/absolute/path/to/retrieval-mcp/target/release/examples/ollama_backend"]' \
  --timeout-seconds 120 \
  --run-id task-001-D --log-file /absolute/path/to/task-001-D.jsonl
```

The server waits for an MCP client on stdin; it is not an interactive terminal application. Stdout carries MCP messages only. Logs go to stderr; `--log-file` additionally appends invocation events to a file whose parent directory must already exist. The file is never truncated. New log files use mode 0600 on Unix.

Default profile is **D**. Without `--semantic-command`, `search_semantic` remains visible in C/D but returns an explicit configuration error. Configure the backend before collecting C/D measurements. `--root` is mandatory and fixed for the session; clients cannot change it through a tool argument.

The SDK is [`rmcp` 3.2.0](https://github.com/modelcontextprotocol/rust-sdk), the official Tokio-based Rust SDK, selected after checking the published crates.io release and upstream documentation. The SDK handles protocol negotiation and stdio; `Cargo.lock` pins the working dependency set. The integration test negotiates MCP `2025-11-25` and exercises real JSON-RPC subprocess calls.

## Connect Claude Code

Build first, then run this command in the repository you want to query. Replace absolute paths:

```sh
claude mcp add --transport stdio --scope local retrieval -- \
  /absolute/path/to/retrieval-mcp/target/release/retrieval-mcp \
  --root /absolute/path/to/repo --profile D \
  --semantic-command '["/absolute/path/to/retrieval-mcp/target/release/examples/ollama_backend"]' \
  --timeout-seconds 120 --log-file /absolute/path/to/retrieval-events.jsonl
```

Use `/mcp` in Claude Code to check the connection. Use `--profile A` and omit the semantic command for the initial grep/read baseline. Claude options belong before the server name; server arguments follow `--`. See [Claude Code's MCP documentation](https://code.claude.com/docs/en/mcp).

## Connect Codex

```sh
codex mcp add retrieval -- \
  /absolute/path/to/retrieval-mcp/target/release/retrieval-mcp \
  --root /absolute/path/to/repo --profile D \
  --semantic-command '["/absolute/path/to/retrieval-mcp/target/release/examples/ollama_backend"]' \
  --timeout-seconds 120 --log-file /absolute/path/to/retrieval-events.jsonl
```

Alternatively, add this to your Codex configuration, replacing the paths:

```toml
[mcp_servers.retrieval]
command = "/absolute/path/to/retrieval-mcp/target/release/retrieval-mcp"
args = ["--root", "/absolute/path/to/repo", "--profile", "D", "--semantic-command", '["/absolute/path/to/retrieval-mcp/target/release/examples/ollama_backend"]', "--timeout-seconds", "120", "--log-file", "/absolute/path/to/retrieval-events.jsonl"]
tool_timeout_sec = 150
```

Use `codex mcp list` to inspect configuration, then start a new session. The examples follow the [official Codex MCP documentation](https://developers.openai.com/codex/mcp). Client setup is documented; the project tests the wire protocol without modifying your agent configuration or running paid model sessions.

## Tools

All tool inputs reject unknown fields. Results include both MCP `structuredContent` and a JSON text equivalent for compatibility, plus output schemas. Line numbers are 1-based and ranges inclusive. Search pages default to 20 results, allow 1–100, and use `offset` / `next_offset`. There is no expensive total-count promise.

| Tool | Use | Example arguments |
|---|---|---|
| `search_exact` | Known text, identifiers, errors, or regex patterns | `{"query":"timeout","path":"src","limit":10}` |
| `read_source` | Inspect or verify a known source location | `{"path":"src/main.rs","start_line":1,"end_line":50}` |
| `find_symbol` | Locate exact-name declarations in Rust/Python | `{"name":"Workspace","limit":10}` |
| `find_callers` | Find likely calls or possible references | `{"name":"resolve","include_references":true,"limit":10}` |
| `search_semantic` | Find behavior when the spelling is unknown | `{"query":"prevent reading files outside the repository","limit":5}` |

`search_exact` is case-sensitive literal search unless `regex:true` or `case_sensitive:false` is supplied. Results represent matching lines, not individual occurrences; excerpts are centered near the first match. Matches remain in stable path/line order for an unchanged repository. It respects ripgrep ignore rules and skips hidden files during traversal; explicit file paths follow ripgrep's explicit-path behavior. It disables ripgrep config files and excludes `.git` and `target` trees during traversal.

`read_source` defaults to 100 lines, allows at most 500 per request, and returns `next_line` when more source remains. The response has a byte budget as well as a line budget. Long individual lines produce an actionable error; use exact search for an excerpt. Reads can explicitly access ignored or hidden regular files within the root.

`find_symbol.path` restricts definitions. `find_callers.path` restricts **call sites**, not target definitions. Caller records include the enclosing symbol, expression, candidate definitions, candidate count, resolution label, confidence explanation, and snippet. At most five candidate definitions accompany each reference; the full count and truncation flag preserve ambiguity. Imports and possible file relationships are bounded context for the returned call-site files.

## Structural indexing and its limits

The first structural invocation builds a full in-memory snapshot using `rg --files` and the Rust/Python Tree-sitter grammars. Subsequent structural calls reuse it. No background watcher, incremental update, database, or persistent index is required. Restart the server to rebuild after edits. Exact search and source reads always query current files.

The index records definitions, imports, function/method call syntax, and optional possible identifier references. Comments and string contents are excluded from reference extraction. Rust `mod` and simple Python imports can produce local-module candidates; Rust `use`, aliases, relative Python imports, re-exports, and unusual layouts can remain unresolved.

Resolution is intentionally conservative: same-language spelling matches produce **candidates**, even if only one definition has that name. Receiver types, scopes/shadowing, package/module lookup, macro expansion, conditional compilation, foreign-language bindings, and runtime dispatch are not resolved. Optional references may include variable bindings and other non-call identifiers. Complex callee expressions can be missed. A name with no matching definition remains `unresolved`; multiple namesakes remain `ambiguous`. No candidate is promoted to a confirmed edge.

Every structural result includes `coverage.complete:false`, supported languages, indexed/unsupported/skipped file counts, bounded skip examples, parse-error count, snapshot timestamp, and freshness/limitation notes. Counts cover files enumerated by ripgrep; hidden/ignored files are not counted. A partially parsed file may contribute results but increments the parse-error count. Empty results never establish that a symbol or caller does not exist.

Budget checks stop adding files after 5,000 indexed files, about 32 MiB of source, about 200,000 records, or the configured indexing time. Per-file limits are 2 MiB, 100,000 named nodes, and syntax depth 128. File-granular aggregate budgets can overshoot by one file. Skips are reported. These ceilings and the full rebuild approach are for small repositories; a larger experiment should replace the index behind `StructuralBackend`.

## Semantic search configuration

The core server launches the configured executable directly with an argument vector, **without a shell**, once per request. It writes one JSON request to stdin, closes stdin, captures a bounded response from stdout, and expects the process to exit. Only the operator can choose this command; tools accept no command, model, endpoint, or executable arguments.

Version-1 request:

```json
{"protocol_version":1,"root":"/absolute/repo","query":"retry failed requests","limit":5,"offset":0}
```

Required response:

```json
{
  "protocol_version": 1,
  "backend": "my-backend/model-version",
  "index_note": "How and when the index was built; known omissions",
  "results": [
    {"path":"src/retry.rs","start_line":10,"end_line":25,"score":0.83}
  ],
  "has_more": false
}
```

Return at most the requested limit in ranked order. With more results, the next offset is the request offset plus the number returned. Use stable ranking for pagination. Scores must be finite; their scale is backend-specific, not a confidence probability. Each hit must name a regular UTF-8 file inside the root with an existing range of at most 500 lines. Invalid paths or stale ranges fail the call. The server reads its own excerpts from current source; it does not trust backend-provided source text. It cannot verify that the backend's ranking reflects current contents.

The subprocess has the operator's permissions: it is a trusted adapter, not a sandbox. The core checks returned paths, but cannot prevent a separately configured program from reading or transmitting other data. Default command timeouts are 30 seconds, configurable from 1 to 600. Backend stdout is capped (1 MiB semantic, 4 MiB lexical); stderr is capped at 64 KiB and not forwarded into tool results. Errors direct the operator to run the adapter itself for diagnostics. Direct child processes are killed on timeout/cancellation; adapters are responsible for any descendants they spawn.

### Persistent Ollama adapter

`examples/ollama_backend.rs` uses local [Ollama's embedding API](https://docs.ollama.com/api/embed) via `curl`. It remains a separate Rust executable at the same path for existing configurations; the `SemanticBackend` interface, subprocess protocol, and MCP API are unchanged.

```sh
# Start Ollama if it is not running, then install the model once if needed:
ollama pull nomic-embed-text
cargo build --locked --release --example ollama_backend
```

Defaults: model `nomic-embed-text`, URL `http://127.0.0.1:11434`. Override with `RETRIEVAL_EMBED_MODEL` / `RETRIEVAL_OLLAMA_URL` in the server's environment. Nomic models receive the documented [query/document prefixes](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5); other models receive plain text. Use the same model for query and document vectors.

The adapter enumerates non-hidden, non-ignored source files with extensions `rs`, `py`, `js`, `jsx`, `ts`, `tsx`, `go`, `c`, `h`, `cpp`, and `java`. It creates overlapping 32-line chunks with a 24-line stride and ranks cosine similarity. It reports skipped files. Document vectors persist across subprocess exits: unchanged path/content chunks reuse embeddings, changed or added chunks are embedded, and deleted chunks are removed. Every request still scans current source and embeds its query.

By default the index lives at `<repository>/.retrieval-mcp/semantic.json`. Set `RETRIEVAL_SEMANTIC_CACHE_DIR` to an absolute directory outside the repository for a read-only checkout. The directory contains one repository/model snapshot at a time; use distinct directories for independent repositories or embedding models. Cache identity includes the canonical repository, endpoint, model name, Ollama model digest, and chunker version. Changing identity invalidates reuse. The model digest is checked around document embedding to avoid publishing mixed-model vectors.

The index stores exact chunk text keys and vectors, so treat it as source code. New snapshot files use temporary files with private permissions, `sync_all`, and atomic replacement. An OS file lock serializes access to each directory (30-second lock wait); process exit releases it. Failed document embedding leaves the previous snapshot intact. Corrupt caches are reported and preserved: move `semantic.json` aside to rebuild. Cache files and the default repository cache directory reject symlinks. Directory-entry durability across power loss is not guaranteed.

The implementation intentionally keeps linear cosine ranking and a 1,000-chunk ceiling, at most 8,000 bytes per chunk and 128 MiB per snapshot. It requests no silent model truncation: an input beyond model context fails explicitly. No model download occurs automatically. For larger corpora, replace the backend with an ANN/vector database. `index_note` reports reused/new embedding counts and the model digest in both tool results and invocation logs.

To exercise real embeddings through MCP (opt-in so ordinary tests stay offline):

```sh
cargo build --locked --example ollama_backend
cargo test --locked --test mcp_stdio real_ollama_semantic_over_stdio -- --ignored --nocapture
```

## Repository boundaries and response limits

All source paths must be relative to a canonical configured directory. Parent traversal, absolute paths, and symlinks in any path component are rejected. Reads require regular UTF-8 files without NUL bytes and cap input at 2 MiB. This protects ordinary local repository use; path validation and subsequent opens are not an OS capability sandbox against a malicious concurrent filesystem replacement. Use an OS sandbox/read-only checkout for adversarial repositories. Source text is untrusted evidence, never an instruction to the agent.

Structured retrieval payloads are capped at 64 KiB. MCP's text compatibility copy means wire responses can be larger; logs separately record payload bytes and serialized MCP result bytes. Requests reaching a tool handler are capped at 16 KiB; the SDK handles transport framing before that check. Overbroad regexes or huge result sets can hit the bounded capture limit before pagination; narrow the query/path. Invalid arguments and expected backend failures return `isError:true`, and every invocation reaching the handler is logged, including disabled tools and schema failures. Malformed JSON-RPC rejected by the SDK is not a tool invocation.

## Logs and routing experiments

Use `--profile` to control the visible and callable tool set:

| Profile | Tools |
|---|---|
| A | exact + read |
| B | exact + read + symbol + callers |
| C | exact + read + semantic |
| D | all five |

Tool descriptions and schemas stay identical across profiles. Disabled tools are absent from `tools/list` and rejected if called by name. No semantic failure silently falls back to grep.

Invocation JSONL has `schema_version:1`, an event (`tool_start` / `tool_end`), epoch-millisecond timestamp, session ID, optional operator run ID, MCP request ID, per-session start sequence, profile, tool name, and argument object. End events add latency, result count, retrieval bytes, MCP response bytes, errors, returned locations, structural coverage, and semantic backend name. Latency includes first-call indexing. Interrupted handlers emit an end event with a cancellation marker; abrupt process termination can leave an unmatched start. Trace diagnostics share stderr but not `--log-file`.

Logs deliberately omit response code bodies, but arguments can still contain sensitive search strings or paths. Keep experiment logs private. They are append-only and flushed through ordinary file writes, without rotation or crash-durable `fsync`. File-write failures are reported on stderr while retrieval continues. Use a separate file per run/process when collecting experiments.

The [benchmark harness and routing analyzer](experiments/README.md) run matched questions under A/B/C/D, retain model transcripts beside server JSONL, and report sequences, fallback/redundancy proxies, source verification, and matched changes in grep/read calls. The harness supports Claude Code and a custom command adapter. It captures client-reported token usage and can exact-match expected answers; routing quality and causal claims still require interpreting the transcript.

## Code layout and replacement points

`src/main.rs` owns startup and stdio. `src/tools` owns MCP schemas and dispatch. `src/search` contains the lexical and semantic interfaces/adapters and bounded subprocess execution. `src/index` owns typed structural results and the Tree-sitter snapshot. `src/source` centralizes paths and bounded reads. `src/logging` owns the versioned event format and append sink. `src/config` owns operator settings.

`LexicalBackend`, `SemanticBackend`, and `StructuralBackend` are the replacement boundaries. Their result types, rather than ripgrep JSON, parser nodes, or embedding vectors, reach MCP. The structural implementation currently owns in-memory storage; a persistent implementation can replace it behind the same interface. The invocation log sink can be changed within `src/logging` without changing tools. There is no speculative storage framework or database migration layer.
