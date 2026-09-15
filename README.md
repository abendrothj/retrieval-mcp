# retrieval-mcp

**Code retrieval for coding agents, with the tool surface chosen by measurement instead of taste.**
Four tools over stdio, for Claude Code, Codex, or anything else that speaks MCP. No index to build,
no database, no daemon, no API key, no embedding service unless you want one. On a sealed held-out
set it matched a specialist code-search MCP on answer quality while the agent spent **24% fewer
input tokens** and carried **34% less context** — [the numbers](#the-result).

## Install

Two commands: get the binary, then tell your client about it.

**1. Get the binary.**

```sh
curl -fsSL https://raw.githubusercontent.com/abendrothj/retrieval-mcp/main/install.sh -o install.sh
sh install.sh
```

Read the script first or do not; piping it —
`curl -fsSL https://raw.githubusercontent.com/abendrothj/retrieval-mcp/main/install.sh | sh` — runs
the same thing, and which of those you are willing to do is your call rather than this project's.
Either way it fetches a published checksum — the release's aggregate `SHA256SUMS`, or the
per-archive `<archive>.tar.gz.sha256` that releases up to and including v0.1.4 carry instead —
verifies the archive for your platform against the single record naming it, and refuses to extract
on a mismatch, a missing record or a duplicate one. It then writes exactly one file —
`retrieval-mcp` in `$HOME/.local/bin`, or wherever `RETRIEVAL_MCP_INSTALL_DIR` points — and nothing
else: no shell startup file, no client configuration, nothing launched, nothing in your repository.
`RETRIEVAL_MCP_VERSION=v0.1.5` installs a specific tag; the default is the latest release.

It registers nothing unless asked. `RETRIEVAL_MCP_REGISTER=claude,codex` runs each client's own
`mcp add` after installing — never an edit to their configuration files, and never a prompt,
because a piped installer's stdin is the script itself and a prompt would silently consume it.
`RETRIEVAL_MCP_SCOPE` picks `local` (this repository, the default, refused outside a git work
tree) or `user` (one entry every project sees). A user-scope entry still reads whichever project
is open, because `--root .` resolves against the directory the client launches the server in:

```sh
RETRIEVAL_MCP_REGISTER=claude,codex RETRIEVAL_MCP_SCOPE=user sh install.sh
```

With a Rust toolchain, or with [`cargo-binstall`](https://github.com/cargo-bins/cargo-binstall) to
fetch the same prebuilt archive without compiling:

```sh
cargo install retrieval-mcp     # builds from source; Rust 1.90+ and a C compiler
cargo binstall retrieval-mcp    # downloads the release archive instead
```

**2. Register it with your client**, run inside the repository you want to query:

```sh
claude mcp add --transport stdio --scope local retrieval -- retrieval-mcp --root .   # Claude Code
codex mcp add retrieval -- retrieval-mcp --root .                                    # Codex
```

That is the installation. Start a new session and the tools are there; `/mcp` in Claude Code or
`codex mcp list` confirms it. Any other MCP client takes the entry directly:

```json
{
  "mcpServers": {
    "retrieval": {
      "command": "retrieval-mcp",
      "args": ["--root", "."]
    }
  }
}
```

MCP servers are declared in configuration and started by the client on demand — you never launch
this yourself, and it exits with the session. `--root` is the only repository the session can read
and is fixed at startup, so a server entry is per-project. It may be relative — resolved once
against the working directory the client launches the server in, which for Claude Code and Codex is
the project you opened — or absolute if you would rather not depend on that. Nothing is written to
your repository, no index is built ahead of time, and the first structural call builds an in-memory
snapshot that dies with the process. [Connect a client](#connect-a-client) has the options worth
adding.

**Manual download.** Prebuilt tarballs are on
[the releases page](https://github.com/abendrothj/retrieval-mcp/releases) — macOS and Linux, x86-64
and arm64 — each with a `.sha256` beside it and all of them together in one `SHA256SUMS`. Verify
against that manifest by hand before extracting, with both files in the same directory:

```sh
archive=retrieval-mcp-v0.1.4-aarch64-apple-darwin.tar.gz
grep "$archive" SHA256SUMS | shasum -a 256 -c -   # sha256sum -c - on Linux; expect "$archive: OK"
mkdir -p ~/.local/bin && tar -xzf "$archive"
install "${archive%.tar.gz}/retrieval-mcp" ~/.local/bin/
```

There is nothing else to install: no runtime dependency, no service, no API key. Search is
ripgrep's own engine linked into the binary, not a `rg` subprocess.

**Optional: the agent skill.** `npx skills add abendrothj/retrieval-mcp` installs
[`skills/retrieval-mcp/SKILL.md`](skills/retrieval-mcp/SKILL.md), a one-page routing guide telling
an agent which of the four tools a given question wants and what each response already answers. The
server advertises the same routing in its own instructions and tool descriptions; the skill is the
copy an agent reads before it calls anything, and nothing depends on it.

### Update and uninstall

Re-run the installer — it verifies the new archive the same way and replaces the binary in place —
or `cargo install --force retrieval-mcp`, or `cargo binstall retrieval-mcp`. Client entries name the
binary rather than a version, so nothing needs re-registering; `retrieval-mcp --version` reports
which build a client is launching. Removal is two commands:

```sh
rm ~/.local/bin/retrieval-mcp   # or: cargo uninstall retrieval-mcp
claude mcp remove retrieval     # or: codex mcp remove retrieval
```

That is the entire footprint: no daemon to stop, no cache or state directory to clear, no index to
delete, no file of any kind left in the repositories you queried. The single exception is opt-in and
named as such — the optional Ollama adapter caches embeddings in `<repository>/.retrieval-mcp/`
unless `RETRIEVAL_SEMANTIC_CACHE_DIR` sent them elsewhere, as
[its documentation](examples/OLLAMA_BACKEND.md) says.

## Quickstart

```sh
# Run it against any checkout, from that checkout, with the installed binary on PATH.
# This is the call your agent will make, and exactly what it gets back:
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"find_callers","arguments":{"name":"build"}}}' \
  | retrieval-mcp --root . 2>/dev/null | tail -1
```

That is the payload an agent receives: every call site of `build` with the definition enclosing it
and a complete count, from one call, against the current files on disk. It is a smoke check — a
wired-up client never needs it. From a clone rather than an install, `cargo build --locked --release
--bin retrieval-mcp` first and call `./target/release/retrieval-mcp` instead.

## What it is

A small Rust MCP server, and the experiment that chose its shape. It can expose seven tools and
defaults to the four that measurably repaid the schema cost of advertising them. There is no LLM
router, no combined search tool, and no automatic fallback: tool descriptions say which tool suits
which question shape, and the model does all the routing.

What produced the result was subtraction rather than sophistication. Dense retrieval is not the
default, hybrid is not the default, there is no server-side routing, and three of the seven tools
were un-defaulted on replicated evidence. What remains is query reformulation by the model, cheap
lexical retrieval, bounded source verification, one relational primitive for facts lexical search
cannot cheaply certify, and an evidence-grounded stopping rule. In this setting interaction design
dominated retrieval sophistication.

```text
Claude Code / Codex / OpenCode
        │ MCP over stdio
        ▼
Rust retrieval server ── invocation events → JSONL
        ├── search_exact       → ripgrep's engine, in process                  [default]
        ├── read_source        → bounded filesystem reads                      [default]
        ├── find_callers       → syntactic references + candidate definitions  [default]
        ├── search_concept     → BM25 over symbol chunks, lexical by default   [default]
        │                          └── optional: local Ollama embeddings instead
        ├── inspect_symbol     → one-hop neighbourhood, both directions, capped
        ├── find_symbol        → Tree-sitter snapshot (Rust, Python, JS/TS, Go, Java, C/C++)
        └── trace_dependencies → bounded transitive call traversal
```

**Where the evidence lives.** This repository is both the server and its research record.
[experiments/README.md](experiments/README.md) holds every finding, its protocol and its caveats:
start at [Findings at a glance](experiments/README.md#findings-at-a-glance), which indexes all of
them in one screen, then [how the project converged](experiments/README.md#how-it-converged) and the
[ledger of measurement defects](experiments/README.md#harness-defect-ledger) found along the way. No
corpus copy or run artifact is published: [`experiments/reproduce_heldout.py`](experiments/reproduce_heldout.py)
rebuilds the pinned corpus from upstream and fails on any mismatch. Release-by-release changes are in
[CHANGELOG.md](CHANGELOG.md).

## The result

Measured once on a sealed held-out set and not tuned against afterwards — 30 questions × 3 arms ×
`claude-sonnet-4-6`, 90 trials, none aborted, frozen four-tool surface, frozen grader:

| | native `Read`/`Grep`/`Glob` | zvec-grep 0.2.2 | **retrieval-mcp** |
|---|---:|---:|---:|
| Correct / 30 | 28 | **29** | **29** |
| Input tokens | 1.15 M | 1.00 M | **764 k** |
| Tool calls | 147 | 78 | **78** |
| Persistent context (tok·turns) | 175 k | 227 k | **150 k** |
| Calls to first evidence | 1.80 | 1.30 | **1.20** |
| Answered without evidence | **0** | **0** | **0** |

**Quality is a tie with zvec-grep and is reported as one** — the arms are discordant on one question
each way — and the context saving is the result: equal quality, fewer turns, less carried context.
Every number came from one model, and none of the findings in this project transferred cleanly
between models, so that is the claim's boundary.
[Paired analysis, the post-hoc sensitivity row, and what the set cost to seal](experiments/README.md#the-held-out-comparison).

Everything else — why the surface is four tools and not seven, why the ranker is BM25 and not
embeddings, why a stopping rule was worth more than any retrieval change, and the four post-freeze
optimisations that were measured and rejected — is in
[Findings at a glance](experiments/README.md#findings-at-a-glance). The one line worth carrying out
of it: **correct evidence reduces recovery turns.** The largest efficiency win measured after the
freeze came from a caller row that started telling the truth about a call site, not from a smaller
payload, a richer result, or more retrieval machinery.

## Tools

The four tools a default session gets are `search_exact`, `read_source`, `find_callers` and
`search_concept`. The catalogue below is all seven; `inspect_symbol`, `find_symbol` and
`trace_dependencies` are available only when named, and why they are not default is in
[Research options](#research-options--not-needed-to-use-the-server).

| Tool | Use | Example arguments |
|---|---|---|
| `search_exact` | Known text, identifiers, errors, or regex patterns | `{"query":"timeout","path":"src","limit":10}` |
| `read_source` | Inspect or verify a known source location | `{"path":"src/main.rs","start_line":1,"end_line":50}` |
| `inspect_symbol` | Both sides of one symbol at a single hop, before choosing a direction | `{"name":"resolve"}` |
| `find_symbol` | Locate exact-name declarations in any indexed language | `{"name":"Workspace","limit":10}` |
| `find_callers` | Find direct calls or possible references | `{"name":"resolve","include_references":true,"limit":10}` |
| `trace_dependencies` | Multi-hop callers, callees, or impact | `{"name":"resolve","direction":"callers","depth":3}` |
| `search_concept` | Find behavior when the spelling is unknown | `{"query":"prevent reading files outside the repository","limit":5}` |

Server `instructions` carry that routing as an explicit table — literals to `search_exact`, unknown
behavior to `search_concept`, declarations to `find_symbol`, relationships to `inspect_symbol`
before a directed tool, direct callers to `find_callers`, transitive relationships to
`trace_dependencies`, and mixed questions to conceptual discovery, then structural lookup, then
`read_source` verification — and tool descriptions repeat the boundary and name the tool to prefer
instead. It is guidance, not enforcement: no tool is required, blocked, or substituted.
[`skills/retrieval-mcp/SKILL.md`](skills/retrieval-mcp/SKILL.md) is the same routing written for an
agent to read before it calls anything.

All tool inputs reject unknown fields. Results include both MCP `structuredContent` and a JSON text
equivalent for compatibility, plus output schemas. Line numbers are 1-based and ranges inclusive.
Search pages default to 20 results, allow 1–100, and use `offset` / `next_offset`. There is no
expensive total-count promise.

`search_exact` is case-sensitive literal search unless `regex:true` or `case_sensitive:false` is
supplied. Results represent matching lines, not individual occurrences; excerpts are centered near
the first match, and matches stay in stable path/line order for an unchanged repository. A `path`
argument narrows the same traversal with a glob rather than starting a new walk, so scoping can
never reach files the unscoped walk would prune. Every page reports `files_searched`: zero over a
nonempty repository means ignore rules or the scope emptied the corpus, not that the text is absent.
It respects ripgrep ignore rules, skips hidden files, excludes `.git` and `target`, and reads no
ripgrep configuration file — the engine is linked in, so a user's `RIPGREP_CONFIG_PATH` cannot
change what a tool call returns.

`read_source` defaults to 100 lines, allows at most 500 per request, and returns `next_line` when
more source remains. The response has a byte budget as well as a line budget. Long individual lines
produce an actionable error; use exact search for an excerpt. Reads can explicitly access ignored or
hidden regular files within the root.

`find_callers` takes an unqualified name. Its `path` restricts **call sites**, not target
definitions, and orientation counts use that same call-site scope. Caller records include the
enclosing symbol, expression, per-reference candidate count, resolution label, confidence
explanation, and snippet. Up to five candidate definitions are stated once per page;
`candidate_definition_count` and `candidate_definitions_truncated` make omissions explicit even when
no caller row exists. Imports and possible file relationships are bounded context for the returned
call-site files.

`search_concept` rows name the enclosing definition; `name_candidate_callers` counts same-language
call sites sharing its unqualified spelling, while `direct_callees` counts call expressions
syntactically owned by that exact definition. Ask for `fields:["excerpt"]` only when source text is
actually needed. Its ranking mechanism is an operator setting, never a model choice:

| Ranker | Implementation | Dependencies | Default |
|---|---|---|---|
| `lexical` | In-process BM25 over definition-shaped chunks | none | yes |
| `semantic` | The configured subprocess backend | an adapter, e.g. Ollama | no |
| `hybrid` | Reciprocal rank fusion (k=60) of both | an adapter | no |

The lexical ranker builds documents from a symbol's path, container, name, split identifier, the
comment block directly above the definition, and its body, with standard BM25 (k1 1.2, b 0.75) and
scope filtering. It needs no model, service, weights, or fetch script, which is why the default
configuration is fully offline. Choosing `semantic` or `hybrid` without `--semantic-command` is a
configuration error rather than a silent downgrade.

The three un-defaulted tools keep their contracts. `inspect_symbol` answers the orientation question
the directed tools make the model guess: one call returns a symbol's definitions, callers, callees
and members, each row labelled with its `relation`, plus `counts` that stay complete when rows are
capped at 3 definitions, 6 per direction and 8 members; an unindexed name returns
`symbol_status:"unknown_symbol"` with `nearest_indexed_names` rather than a silent empty page.
`find_symbol.path` restricts definitions. `trace_dependencies` walks call syntax inbound
(`direction:"callers"`) or outbound (`direction:"callees"`), 3 hops by default and 5 at most,
expanding each name once so recursive code terminates; because names are unqualified, distinct
namesakes merge into one traversal node, so verify material edges with `read_source`.

## Limits that change your answer

**Empty results never establish that a symbol or caller does not exist.** Resolution is
intentionally conservative: same-language spelling matches produce **candidates**, even when only
one definition has that name, and no candidate is ever promoted to a confirmed edge. Receiver types,
scopes and shadowing, package/module lookup, macro expansion, conditional compilation,
foreign-language bindings and runtime dispatch are not resolved. Optional references may include
variable bindings and other non-call identifiers, and complex callee expressions can be missed. A
name with no matching definition stays `unresolved`; multiple namesakes stay `ambiguous`.

**`coverage.budget_truncated` is the field that changes how an answer should be written.**
`coverage.complete` is always false — syntactic resolution is never complete — so on its own it
cannot distinguish "matching is approximate" from "the index stopped before scanning the
repository". When `budget_truncated` is true, whole files were never read, `indexed_files` against
`eligible_files` says how many, and the routing instructions tell the model not to answer an
exhaustive question from that snapshot as though absence were proven. Every structural result also
carries the supported languages, indexed/eligible/unsupported/skipped counts, bounded skip examples,
parse-error count, snapshot timestamp and freshness notes; index-backed `search_concept` responses
carry the same `indexed_files` count.

**What is indexed.** The first structural invocation builds a full in-memory snapshot by walking the
repository with ripgrep's `ignore` crate — the same walk `search_exact` uses, so both describe one
corpus — and parsing it with the Rust, Python, TypeScript/TSX, Go, Java, C and C++ Tree-sitter
grammars. JavaScript and TypeScript are one family: `.js`, `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx`,
`.mts` and `.cts` are indexed together, a `.js` call site resolves against a `.ts` definition, and
an ESM specifier written `./util.js` resolves to `util.ts`. The index records definitions, imports,
call syntax and optional identifier references; comments and string contents are excluded from
reference extraction. A constructor invocation is a call site — `new Table(rows)` in JavaScript,
TypeScript, Java or C++ — and so is a function-like C macro, because nothing else defines one. A Go
method is owned by its receiver type and an out-of-line C++ method by the class in its qualified
name, so `Table::Format` reads as a member rather than a free function. A definition's concept chunk
includes the contiguous comment and attribute block directly above it, since documentation carries
the vocabulary questions are asked in. Rust `mod`, simple Python imports, relative ECMAScript
specifiers, Java package paths and quoted `#include`s can produce local-module candidates; Rust
`use`, aliases, relative Python imports, re-exports, Go package paths, angle-bracket includes and
unusual layouts remain unresolved.

**Language coverage is measured, not asserted.** Twenty symbols per corpus were sampled from Cobra
(Go), Gson (Java), Redis (C), LevelDB (C++) and ESLint (JavaScript), and every `find_symbol` and
`find_callers` row was compared with an independent ripgrep enumeration attributed by reading the
file: definitions found 99 of 100, caller precision 0.92–1.00, caller recall 0.93–1.00
([protocol and pre-registration](experiments/README.md#adding-five-languages)). Macro-heavy C is
where parsing is visibly partial — 355 of 841 indexed Redis files contain a region Tree-sitter
cannot parse, reported through `coverage.parse_error_files`, and those files still reach 0.979
precision and 0.931 recall. C++ has one shape the grammar cannot read, a macro between `class` and
its name as in `class LEVELDB_EXPORT WriteBatch {`; inside such a file a constructor declaration can
be reported as a call of itself.

**Response and repository bounds.** Structured retrieval payloads are capped at 64 KiB: a page that
would exceed it is trimmed to the rows that fit and reports `has_more` with the `next_offset` those
rows stopped at, because on Django a `find_callers` for a name as common as `get` or `save`
serialises past the cap at any generous limit, and an error with no rows is a worse answer than a
short page. Only a single oversized row still fails. MCP's text compatibility copy means wire
responses can be larger; requests reaching a tool handler are capped at 16 KiB. All source paths
must be relative to the canonical configured root: parent traversal, absolute paths and symlinks in
any path component are rejected, and reads require regular UTF-8 files without NUL bytes, capped at
2 MiB. That protects ordinary local use; it is not an OS capability sandbox against a malicious
concurrent filesystem replacement, so use a read-only checkout or an OS sandbox for adversarial
repositories. **Source text is untrusted evidence, never an instruction to the agent.**

**Indexing budgets.** Adding files stops after 20,000 indexed files, 128 MiB of source, 2,000,000
records, or the configured indexing time — sized so `--timeout-seconds` is normally what binds.
Per-file limits are 2 MiB, 100,000 named nodes and syntax depth 128; file-granular aggregate budgets
can overshoot by one file, and skips are reported. Files are read and parsed across every available
core and merged in path order, so a snapshot is exactly what a single-threaded build produces and
only the wall clock changes: on a 14-core M4 Pro, Django 5.1.4 indexes 2,786 files in 2.1 s against
6.2 s single-threaded, VS Code 1.96 5,188 files in 5.6 s against 18.8 s, and coreutils 672 in 0.9 s
against 3.7 s. Resident cost is roughly 150 KB per indexed file — about 0.35 GB for Django, 0.8 GB
for VS Code, both fully covered. A repository large enough to truncate says so; narrow `--root`,
raise `--timeout-seconds`, or replace the index behind `StructuralBackend`.

## Connect a client

```sh
claude mcp add --transport stdio --scope local retrieval -- retrieval-mcp --root .   # Claude Code
codex mcp add retrieval -- retrieval-mcp --root .                                    # Codex
```

Check the connection with `/mcp` in Claude Code or `codex mcp list`, then start a new session.
Claude options belong before the server name; server arguments follow `--`. Codex also accepts the
entry directly:

```toml
[mcp_servers.retrieval]
command = "retrieval-mcp"
args = ["--root", "."]
tool_timeout_sec = 150
```

Substitute an absolute `--root` if you would rather not depend on the directory the client launches
the server in, and an absolute path to `./target/release/retrieval-mcp` if you built from a clone.
The options worth knowing:

| Option | Effect |
|---|---|
| `--root <dir>` | Mandatory; the only repository the session can read, canonicalised once at startup |
| `--timeout-seconds <n>` | 1–600, default 30; what normally bounds indexing on a large repository |
| `--log-file <path>` | Append invocation events as JSONL; the parent directory must already exist |
| `--run-id <id>` | Operator label recorded in every event, for matching a run to its trials |
| `--no-ignore` | Search and index ignored files too; `.git`, `target` and hidden files stay excluded |
| `--tools <list>` | Restrict the session's tools — see [Research options](#research-options--not-needed-to-use-the-server) |
| `--ranker <lexical\|semantic\|hybrid>` | Ranking behind `search_concept`; the last two need `--semantic-command` |

The server waits for an MCP client on stdin; it is not an interactive terminal application. Stdout
carries MCP messages only, and logs go to stderr. Concurrent calls are answered independently:
requests are handled as they arrive, the structural snapshot is built once behind a `OnceCell`
however many callers race it, and every accepted request is answered even if the client closes stdin
mid-build — pinned by four stdio tests that pipeline 24 calls without awaiting replies, race eight
structural calls against one lazy build, cancel a call while its build runs, and close stdin with
nine requests in flight. Client setup follows the official
[Claude Code](https://code.claude.com/docs/en/mcp) and
[Codex](https://developers.openai.com/codex/mcp) MCP documentation; the project tests the wire
protocol without modifying your agent configuration or running paid model sessions.

**Invocation logs.** `--log-file` writes JSONL with `schema_version:1`, an event
(`tool_start` / `tool_end`), epoch-millisecond timestamp, session ID, optional run ID, MCP request
ID, per-session sequence, the tool set, tool name and argument object. End events add latency
(including first-call indexing), result count, retrieval bytes, MCP response bytes, errors, returned
locations, structural coverage and semantic backend name. Interrupted handlers emit a cancellation
marker; abrupt termination can leave an unmatched start. Files are append-only, never truncated,
mode 0600 on Unix, without rotation or crash-durable `fsync`; write failures are reported on stderr
while retrieval continues. Response bodies are omitted, but arguments can still contain sensitive
search strings or paths — keep experiment logs private, and use a separate file per run.

## Build, test, and code layout

Building requires Rust 1.90+ (tested with 1.96) and a C compiler for Tree-sitter. The binary needs
nothing at runtime: ripgrep's `grep-searcher`, `grep-regex` and `ignore` crates are linked in, so
there is no `rg` subprocess and no PATH dependency. The SDK is
[`rmcp` 3.3.0](https://github.com/modelcontextprotocol/rust-sdk), the official Tokio-based Rust SDK;
`Cargo.lock` pins the working dependency set, and the integration test negotiates MCP `2025-11-25`
over real JSON-RPC subprocess calls.

```sh
cargo build --locked --release --bin retrieval-mcp
cargo test --locked --all-targets            # 29 library, 14 stdio (1 ignored), 6 example tests
cargo clippy --locked --all-targets -- -D warnings
python3 -W error::ResourceWarning -m unittest discover -s experiments -p 'test_*.py'   # 215 tests
python3 experiments/check_docs.py            # the docs still describe the server that exists
```

All of these run offline and call no model. The Python suite exercises the harness itself: real MCP
transport against a fixture server, the budget gate, the provider-failure abort, the answer
resolver, and every question set's internal consistency. The one test that needs a live Ollama is
ignored by default; run it with
`cargo test --locked --test mcp_stdio real_ollama_semantic_over_stdio -- --ignored`.

```text
src/            the server: config, tools, search (lexical/BM25/semantic), index, source, logging
examples/       ollama_backend.rs, the reference semantic adapter, plus its cache support
tests/          mcp_stdio.rs, real JSON-RPC subprocess tests over the wire protocol
experiments/    the study harness: runners, graders, analyzers, question sets, and their tests
```

`src/main.rs` owns startup and stdio, `src/tools` the MCP schemas and dispatch, `src/search` the
lexical and semantic interfaces and bounded subprocess execution, `src/index` the typed structural
results and Tree-sitter snapshot, `src/source` paths and bounded reads, `src/logging` the versioned
event format, `src/config` operator settings. `LexicalBackend`, `SemanticBackend` and
`StructuralBackend` are the replacement boundaries: their result types, rather than ripgrep JSON,
parser nodes or embedding vectors, are what reaches MCP, and the in-memory structural implementation
can be replaced by a persistent one behind the same interface. There is no speculative storage
framework or database migration layer.

Everything under `experiments/` is Python 3.11+ standard library only. `comparison_runner.py`
prepares and runs multi-arm agent comparisons; `comparison_gate.py` meters one shared call and byte
budget across a trial's MCP upstreams; `codex_wrapper.py` and `opencode_wrapper.py` drive the two
command-line clients and `--client claude` drives Claude Code directly; `validate_suite.py` compiles
a question suite against the corpus, `tool_reachability.py` proves a supported tool path reaches
each gold, and `lexical_oracle.py` rejects questions a bounded search-and-read crawl dissolves;
`quality_pass.py` resolves an answer's spelling against definitions found by ripgrep; `end_to_end.py`
scores context consumption for OpenCode, Codex and Claude streams alike; `closure_audit.py`
classifies every loss a stopping policy causes; `schema_ablation.py` prices each tool's schema
against its observed utility; and `study_a.py` and `study_b.py` run the offline retrieval studies.
Every runner that can spend money requires `--allow-model-usage` and refuses to overwrite an
existing output.

Corpora, run artifacts, transcripts and model answers are deliberately absent: trial directories
hold full prompts, model reasoning and verbatim source excerpts from whatever corpus was under test,
so they stay local. A published result ships the pinned question sets, gold answers and analysis
JSON, plus a script that clones the pinned upstream revision — not the corpus copy.

## Research options — not needed to use the server

Everything above is the default surface: four tools, an in-process lexical ranker, no configuration.
This section is the rest of the instrument. It exists because the default was **chosen by
measurement**, and the measurements have to stay runnable: `--tools` is how any arm of an ablation
is built, the profile letters replay the original availability study, and the semantic backend is
the subject of a published finding rather than a recommendation. None of it costs a default session
anything — an un-exposed tool is absent from `tools/list` and its schema is never sent.

### Restricting the tool set

The server can expose seven tools, and defaults to the four that repaid their schema cost in the
tool trials: `search_exact`, `read_source`, `find_callers`, `search_concept`. `inspect_symbol`,
`find_symbol` and `trace_dependencies` are un-defaulted, not removed — name them in `--tools` to get
them back, singly or together, which is also how an ablation arm is built:

```sh
# The full seven-tool surface, named rather than presumed.
./target/release/retrieval-mcp --root /path/to/repo \
  --tools search_exact,read_source,inspect_symbol,find_symbol,find_callers,trace_dependencies,search_concept

# Is inspect_symbol earning its place? Remove it and change nothing else.
./target/release/retrieval-mcp --root /path/to/repo \
  --tools search_exact,read_source,find_symbol,find_callers,trace_dependencies

# Grep-and-read baseline: two tools, no structural index and no concept search.
./target/release/retrieval-mcp --root /path/to/repo --tools search_exact,read_source
```

A tool that is not listed is absent from `tools/list` and refused if called by name. Descriptions
and schemas are identical however a tool was enabled, no failure silently falls back to another
tool, and unknown names are rejected at startup rather than ignored.

`--profile A|B|C|D` is retained **only** so commands recorded by the original availability study
replay unchanged. It is not a setting for new work; use `--tools`.

| Profile | Equivalent `--tools` |
|---|---|
| A | `search_exact,read_source` |
| B | `search_exact,read_source,inspect_symbol,find_symbol,find_callers,trace_dependencies` |
| C | `search_exact,read_source,search_concept` |
| D | all seven |

The two switches are mutually exclusive: passing both is an error, because the invocation log
records one name for the tool set. The letters describe an availability experiment that is finished;
every study since has named tools directly.

### Semantic backend protocol

The core server launches the configured executable directly with an argument vector, **without a
shell**, once per request. It writes one JSON request to stdin, closes stdin, captures a bounded
response from stdout, and expects the process to exit. Only the operator can choose this command;
tools accept no command, model, endpoint, or executable arguments.

```json
{"protocol_version":1,"root":"/absolute/repo","query":"retry failed requests","limit":5,"offset":0}
```

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

Return at most the requested limit in ranked order; with more results, the next offset is the
request offset plus the number returned, and ranking must be stable across pages. Scores must be
finite, and their scale is backend-specific rather than a confidence probability. Each hit must name
a regular UTF-8 file inside the root with an existing range of at most 500 lines; invalid paths or
stale ranges fail the call. The server reads its own excerpts from current source and does not trust
backend-provided text, but it cannot verify that a backend's ranking reflects current contents.

The subprocess has the operator's permissions: it is a trusted adapter, not a sandbox. Command
timeouts default to 30 seconds and range from 1 to 600. Backend stdout is capped (1 MiB semantic,
4 MiB lexical); stderr is capped at 64 KiB and not forwarded into tool results, and errors direct
the operator to run the adapter itself for diagnostics. Direct child processes are killed on timeout
or cancellation; adapters are responsible for any descendants they spawn.

[`examples/ollama_backend.rs`](examples/ollama_backend.rs) is the reference implementation, a local
Ollama embedding adapter with a persistent vector cache. Its model settings, chunking, cache
identity, file format and durability guarantees are documented in
[`examples/OLLAMA_BACKEND.md`](examples/OLLAMA_BACKEND.md).
