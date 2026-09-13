# retrieval-mcp

**Code retrieval for coding agents, with the tool surface chosen by measurement instead of taste.**
Four tools over stdio, for Claude Code, Codex, or anything else that speaks MCP. No index to build,
no database, no daemon, no API key, no embedding service unless you want one. On a sealed held-out
set it matched a specialist code-search MCP on answer quality while the agent spent **24% fewer
input tokens** and carried **34% less context** — [the numbers](#what-the-experiments-found).

## Install

```sh
cargo install retrieval-mcp
```

Then register it with your client. MCP servers are declared in configuration and started by the
client on demand — you never launch this yourself, and it exits with the session:

```json
{
  "mcpServers": {
    "retrieval": {
      "command": "retrieval-mcp",
      "args": ["--root", "/absolute/path/to/your/repo", "--timeout-seconds", "120"]
    }
  }
}
```

`--root` is fixed at startup and is the only repository the session can read, so a server entry is
per-project. [Connect Claude Code](#connect-claude-code) and [Connect Codex](#connect-codex) give the
one-liners that write this entry for you. Nothing is written to your repository, no index is built
ahead of time, and the first structural call builds an in-memory snapshot that dies with the process.

## Quickstart

```sh
cargo build --locked --release --bin retrieval-mcp

# Make the call your agent will make, and see exactly what it gets back:
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"find_callers","arguments":{"name":"build"}}}' \
  | ./target/release/retrieval-mcp --root "$PWD" 2>/dev/null | tail -1
```

That is the payload an agent receives: every call site of `build` with the definition enclosing it
and a complete count, from one call, against the current files on disk. It is a smoke check from a
clone — a wired-up client never needs it.

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
        ├── search_exact       → ripgrep subprocess                          [default]
        ├── read_source        → bounded filesystem reads                    [default]
        ├── find_callers       → syntactic references + candidate definitions [default]
        ├── search_concept     → BM25 over symbol chunks, or a semantic/hybrid ranker
        │                          └── optional: local Ollama embeddings     [default]
        ├── inspect_symbol     → one-hop neighbourhood, both directions, capped
        ├── find_symbol        → Tree-sitter snapshot (Rust/Python/TypeScript)
        └── trace_dependencies → bounded transitive call traversal
```

Design decisions here are downstream of measurements; the protocol, artifacts, and caveats behind every claim are in [experiments/README.md](experiments/README.md).

**Where the evidence lives.** This repository is both the server and its research record: [experiments/README.md](experiments/README.md) holds the held-out comparison against zvec-grep and a native control, [how the project converged](experiments/README.md#how-it-converged), the reasoning that cut the surface from seven tools to four, and a [ledger of the measurement defects](experiments/README.md#harness-defect-ledger) found along the way. No corpus copy or run artifact is published: [`experiments/reproduce_heldout.py`](experiments/reproduce_heldout.py) rebuilds the pinned corpus from upstream and fails on any mismatch.

## What the experiments found

**The headline, measured once on a sealed held-out set and not tuned against afterwards** — 30 questions × 3 arms × `claude-sonnet-4-6`, 90 trials, frozen four-tool surface, frozen grader:

| | native `Read`/`Grep`/`Glob` | zvec-grep 0.2.2 | **retrieval-mcp** |
|---|---:|---:|---:|
| Correct / 30 | 28 | **29** | **29** |
| Input tokens | 1.15 M | 1.00 M | **764 k** |
| Tool calls | 147 | 78 | **78** |
| Persistent context (tok·turns) | 175 k | 227 k | **150 k** |
| Calls to first evidence | 1.80 | 1.30 | **1.20** |
| Answered without evidence | **0** | **0** | **0** |

Quality is a tie with zvec and is reported as one; the context saving is the result. Details, paired analysis and the post-hoc sensitivity row: [The held-out comparison](experiments/README.md#the-held-out-comparison). Everything below explains how the surface that produced it was chosen.

Each finding names the model it came from, because none of them transferred cleanly between models. Protocol, artifacts, and caveats: [experiments/README.md](experiments/README.md). A one-screen index of every result is [Findings at a glance](experiments/README.md#findings-at-a-glance).

**Description questions are a vocabulary problem, not an embedding problem.** A model-free ranker bake-off on 18 gradable coreutils questions put dense retrieval well ahead of BM25: recall@5 of 12/18 semantic and 13/18 hybrid against 6/18 lexical. On a separate authored VS Code suite the same three rankers ran twice — once on the raw question, once on a frozen one-shot rewrite of it into code vocabulary. Rewriting moved BM25 from 3/15 to 9/15 at recall@5 (MRR 0.167 → 0.484) while dense moved 2/15 to 4/15 (0.144 → 0.273), so reformulated BM25 beat dense on every metric, reformulated or not. The expensive semantic work pays off in *query formation*, not in document ranking, which is why `lexical` is the default ranker and embeddings are an optional backend.

**Grep-first habits are model-specific.** Running the same coreutils suite inside a real agent loop, DeepSeek V4 Flash scored 20/22 while rarely calling the semantic tool at all. Retrieval-engine differences were marginal in a tool-rich agent; corpus naming conventions dominated. Conclusions from an offline ranker bake-off do not survive contact with a different model's habits.

**Guessing traversal direction was the largest remaining failure.** A 90-decision A/B of the tool surface found the model already anchored on the correct symbol in 26 of 40 structural decisions and still failed, because it asked for callers when the answer lay among callees. Offering `inspect_symbol` instead — 45 decisions per surface, three repetitions, nothing else changed — cut `wrong_reach` from 15 to 4 of 44 decisions and raised certified solves from 17 to 27 of 45, with six questions improving and none regressing. Container-versus-member confusion (`wrong_level`, 9 → 8) did **not** improve: naming the level is not the same as choosing it.

**End-to-end against real competitors, the advantage does not yet appear.** Fifteen authored VS Code questions × two repetitions × three arms — OpenCode's native grep/read, [zvec-grep](https://github.com/zvec-ai/zvec-grep) 0.2.2, and this server — on one identical 444-file TypeScript corpus, driven by Codex CLI with `gpt-5.6-luna`:

| | native | zvec-grep | retrieval-mcp |
|---|---:|---:|---:|
| Resolved correct / 30 | 14 | 15 | 16 |
| Median input tokens | 206,054 | 213,646 | 227,955 |
| Median retrieval bytes | 92,449 | 61,756 | 85,231 |
| Input tokens, resolved trials only | 177,513 | 190,156 | 162,862 |
| Retrieval bytes, resolved trials only | 92,665 | 60,188 | 52,879 |

Quality is a tie: the paired discordance is one to two question-repetitions. Total context is *worse* here, about 10% above the native baseline. Conditioned on succeeding, this server is the cheapest arm on both tokens and bytes; it is the most expensive when it fails, spending 46 KB after the gold evidence was already on screen against 14 KB for zvec. That is a stopping-criterion problem, not a retrieval-quality one.

**Most of that suite's hard questions were broken, not hard.** Auditing the 7 questions no arm ever solved found no tool-surface gap and at most one retrieval failure: two golds are factually wrong against a ripgrep enumeration of the corpus — in one case a trial listed all 11 true callers and scored 0 against a gold naming 3, one of which calls nothing — three fail on answer shape or an ambiguous name, and one question admits two defensible answers. Repairing the golds and the grader lifts the arms to 19/18/17 of 30 (this server, zvec, native) with paired discordance of 3:1 and 2:1 in this server's favour, still far too small to claim on 15 questions, and 4 of 15 questions now discriminate at all. Suites are therefore compiled before use: `validate_suite.py` verifies every gold against the corpus and every grader expectation against synthetic answers, and exits nonzero otherwise.

**The harness is the second experimental subject.** Fifteen defects in it have produced or nearly produced believable false findings — a grader that scored notation instead of retrieval, a vector cache written inside the corpus under test, swallowed MCP tool errors, an output-file check that ran after the model spend, a resolver blind to TypeScript, a set grader that rejected prose, indexers that missed multi-modifier methods and Python classes, three separate call-site attribution flaws, a context scorer that read zero for Claude arms, and a grader that scored a keyed answer zero while crediting the same symbols in prose. They run in both directions: some flattered this server, some penalised it, and the last was found inside its own held-out loss. The full list, with what each would have shown, is the [defect ledger](experiments/README.md#harness-defect-ledger); each is pinned by a test.

**The newest suite does not discriminate yet.** A 60-question Django 5.1.4 suite — 30 development, 30 held-out, compiled clean by `validate_suite.py` — was piloted on its development half with `gpt-5.6-luna`. Under the repaired grader all three arms answer every completed trial correctly, so the questions separate nothing, and this server spends 40% more input tokens and 2.75× the bytes after first hit than plain grep-and-read to reach the same answers. The held-out half stays unrun until the development half is made hard enough to discriminate.

**Knowing when to stop was worth more than any retrieval change.** Input tokens track *turns*, not payload bytes — across 67 trials the correlation with call count is 0.74 and with retrieval bytes 0.09, because every call re-sends the transcript at 40–45 k input tokens. Hand-labelling the 50 post-first-hit calls in the ten most wasteful traces found 39 of them unnecessary, dominated by re-reading source for callers `find_callers` had already named with their files. One `prompt_policy` stopping rule, phrased entirely in the completeness signals the server already emits (`counts`, `has_more`, relation labels), cut calls 155 → 87 and input tokens 6.73 M → 5.27 M over 30 questions with correctness unchanged at 30/30. On the 14 questions with a three-arm baseline it now makes the fewest calls of any arm (33 against 45 native and 55 zvec) and carries the least context after first hit (82 KB against 100 and 111 KB), narrowing its token overhead against plain grep-and-read from +40% to +8%. Measured where no arm fails, so it cannot yet be shown to cost accuracy.

**The residual overhead is the tool surface, not the payload.** `end_to_end.py` now scores `context_token_turns` — a payload's tokens times the number of later turns that must carry it, computed the same way for MCP and native arms. After the stopping rule this server carries 140 k tok·turns of persistent payload against the native control's 226 k and zvec's 390 k, so it is already the leanest arm on payload while still spending 8% more input tokens. Seven tool schemas at ~5,786 tokens, re-sent on every one of 47 turns, over-explain the whole difference. Shrinking `search_concept` was costed from 51 archived responses before being built: withholding rows 4+ saves 1.72% of input tokens and would cost about 1.98% in expansion turns, both an order of magnitude inside the ~5% run-to-run floor, so it was not run and the response shape was left alone.

**The stopping rule survives a suite built to punish it.** A third development set of 30 questions was authored against a difficulty contract — first plausible symbol is a distractor, answer needs two definitions combined, two-hop traversal, or namesake discrimination — and `closure_audit.py` was written to catch the failure mode that matters: a treatment loss where a required fact never appeared in any tool result. On that suite the rule still cuts calls 187 → 101 (−46%), input tokens 7.15 M → 5.70 M (−20%) and persistent context load by 74%, for one lost answer in thirty. That loss is a premature *commit*, not a premature stop: both gold markers were already on screen and the model chose the neighbouring property. Zero premature stops, zero skipped expansions. The suite's own limit is that it still does not discriminate the control, which scored 30/30.

**The one remaining loss was ambiguity, and one sentence fixed it.** Adding *"if multiple visible symbols plausibly satisfy a requested fact, disambiguate between those candidates before answering"* to the stopping rule restored both losses on the hard suite — 30/30 — while cutting tool calls 100 → 96 and post-first-hit calls 33 → 30, with zero regressions under `closure_audit.py`. Input tokens rose 8%, dominated by a single trial that spent eight extra reads confirming a caller list, so that is a caveat rather than a saving. The closure policy is now frozen.

**The schema tax is real; the capability pressure justifying it is not.** Six arms — the full surface, one leave-one-out arm per structural tool, and a lexical-only arm — over 12 questions authored for caller orientation, two-hop traversal, namesakes and interface resolution, with a mechanically verified tool path to every gold (`tool_reachability.py`), a shared five-call ceiling and `claude-sonnet-4-6`: every arm scored 11/12, no structural tool enabled a single unique solve, and `trace_dependencies` was called zero times even on the six two-hop questions. The full surface was the most expensive arm on every axis — 341 k input tokens and 208 k schema tok·turns against the lexical-only arm's 277 k and 62 k. The surface is *not* being cut on that evidence: it shows only that **tool reachability is not tool necessity**, because a lexical seed plus narrow reads also reached the gold inside the budget. The next compilation property is necessity, not reachability.

**One structural tool now pays for itself; the rest still do not.** The schema ablation's real lesson was that reachability is not necessity, so `lexical_oracle.py` was built to enforce the difference: a deterministic, model-free crawl using only search and reads, which starts from the question, may never query a gold identifier, and must recover the complete gold within the treatment's own budget. It dissolves 12 of 30 development questions, 14 of 30 hard questions, and 6 of the 12 retrieval-strategy questions — most in a single call. A suite that survives it was then built on what lexical hits do not encode: a hit carries a path and a line, never the definition enclosing it, so ten questions demand every enclosing caller of a helper, exhaustively, across four to five files. On those, adding `find_callers` to a lexical surface cut calls 77 → 29 and input tokens 679 k → 298 k, paying 45.6 k tokens of schema to avoid 280.4 k — **net +234.8 k**. The full seven-tool surface was still beaten by that four-tool one on every cost axis at equal correctness, so the minimal defensible surface today is `search_exact`, `search_concept`, `read_source` and one relational primitive; each remaining tool now owes the same proof.

**Premature stopping is now a permanent safety column.** `end_to_end.py` reports `answered_without_evidence` and `wrong_without_evidence` for every arm of every run: trials where a required gold identity appeared in no tool result at all. Closure's runs score 0 on every arm, agreeing with `closure_audit.py`; the necessity suite immediately showed the lexical-only arm answering without evidence three times against one for each structural arm. An efficiency change that moves this number is a regression whatever the token columns say.

**The default surface is now four tools, decided by measurement.** With the pruning rule made explicit — a tool stays only if it has an oracle-resistant task class *and* saves more context than its schema costs — each remaining tool was given its own class and its own arm. `trace_dependencies` was called **zero** times across three separate experiments, including on two-hop questions authored for it that survive a crawl which already has `find_callers`, while carrying the largest schema of any tool. `inspect_symbol` and `find_symbol` were then replicated three times on their own claimed classes, 63 trials: the baseline scored 21/21 on both classes, `inspect_symbol` was called once in 21 trials for a net −67.8 k tokens, and `find_symbol` was used fourteen times, lost a question in all three repetitions, and still came out at −24.8 k. Neither produced a single unique solve where it was supposed to. So with no `--tools` and no `--profile` the server now exposes `search_exact`, `read_source`, `find_callers` and `search_concept`; the other three are un-defaulted rather than removed, and `--profile D` still replays every command in this study.

**The held-out set answered the question the project started with.** Opened once, after the architecture, closure policy and tool surface were frozen; 30 sealed questions × 3 arms × `claude-sonnet-4-6`, 90 trials, none aborted. Against zvec-grep this server scored the same **29 of 30** on the same **78 tool calls**, for **764 k input tokens against 1.00 M (−24%)**, **150 k against 227 k tok·turns of persistent payload (−34%)**, first evidence in 1.20 calls against 1.30, and a 10.6 s median trial against 11.7 s; against the native `Read`/`Grep`/`Glob` control, 28 of 30 at 1.15 M tokens and 147 calls. No arm answered without evidence. Paired against zvec the arms are discordant on one question each way — a tie, and reported as one. **Quality indistinguishable, context materially lower: an MCP can give a coding agent equal quality with fewer turns and less total context than grep-and-read.** A sixteenth harness defect surfaced in this server's own single loss — a keyed object naming both gold symbols scored 0 where prose naming them scored 1.0 — and after repairing it symmetrically and regrading all three arms, retrieval-mcp is 30/30, native 29/30, zvec 29/30. The frozen-grader row is the primary result; the set is now spent.

**Three plausible index changes, one survivor.** After the surface was frozen, driving the server against an awkward repository produced three index changes — doc comments in the concept chunk, markdown files chunked by heading into the same index, and calls written inside Rust macro arguments — all cheap, all plausible, all built before anything was measured. `study_a.py` graded them offline in 30 s of CPU with no model calls. Doc comments in the chunk moved the authored coreutils suite from 7/18 to 10/18 at recall@5 and 0.246 to 0.474 MRR, left Django untouched (Python docstrings already sit inside the definition), and were kept. Markdown chunking was unmeasurable on every suite in this repository, because not one of the eleven measured corpora holds a single markdown file; on the upstream coreutils checkout, which holds 76, markdown chunks took 14 of 180 result slots, pushed one gold out of the top ten and improved nothing, so it was reverted. Macro-argument calls did recover real call sites, but 347 of the 361 rows they added over 40 sampled symbols were assertions inside test functions, and both graded Rust caller questions exclude test code — a 47% payload increase on `find_callers`, the one structural tool with a ledger to protect — so it was reverted too. [Detail and artifacts](experiments/README.md#three-index-changes-one-survivor).

## Build and run

Requires Rust 1.90+ (tested with 1.96), a C compiler for Tree-sitter, and `rg` on `PATH`. The optional Ollama adapter also requires `curl` and a running Ollama service with an embedding model.

```sh
cd /path/to/retrieval-mcp
cargo build --locked --release --bin retrieval-mcp --example ollama_backend
cargo test --locked --all-targets
cargo clippy --locked --all-targets -- -D warnings

# Default four tools, in-process BM25 behind search_concept: no embedding model, no service, no network.
./target/release/retrieval-mcp --root /absolute/path/to/repo \
  --run-id task-001 --log-file /absolute/path/to/task-001.jsonl

# Grep-and-read baseline: two tools, no structural index and no concept search.
./target/release/retrieval-mcp --root /absolute/path/to/repo --tools search_exact,read_source

# Embeddings behind search_concept instead of BM25.
./target/release/retrieval-mcp --root /absolute/path/to/repo --ranker semantic \
  --semantic-command '["/absolute/path/to/retrieval-mcp/target/release/examples/ollama_backend"]' \
  --timeout-seconds 120
```

The server waits for an MCP client on stdin; it is not an interactive terminal application. Stdout carries MCP messages only. Logs go to stderr; `--log-file` additionally appends invocation events to a file whose parent directory must already exist. The file is never truncated. New log files use mode 0600 on Unix.

Four tools are exposed by default — `search_exact`, `read_source`, `find_callers`, `search_concept` — and the default ranker is **lexical**, so a plain `--root` invocation is fully offline. That default is the measured one, not the maximal one: see [Restricting the tool set](#restricting-the-tool-set) for the other three and the profile presets. `--ranker semantic` or `--ranker hybrid` requires `--semantic-command`; without it, `search_concept` returns an explicit configuration error rather than silently degrading to a different ranking. `--root` is mandatory and fixed for the session; clients cannot change it through a tool argument. Ignore files are honored by default; `--no-ignore` searches and indexes ignored files too, for repositories whose ignore rules hide the code under study (a nested repository ignored by its parent, say). `.git`, `target` and hidden files stay excluded either way.

The SDK is [`rmcp` 3.2.0](https://github.com/modelcontextprotocol/rust-sdk), the official Tokio-based Rust SDK, selected after checking the published crates.io release and upstream documentation. The SDK handles protocol negotiation and stdio; `Cargo.lock` pins the working dependency set. The integration test negotiates MCP `2025-11-25` and exercises real JSON-RPC subprocess calls.

## Connect Claude Code

Build first, then run this command in the repository you want to query. Replace absolute paths:

```sh
claude mcp add --transport stdio --scope local retrieval -- \
  /absolute/path/to/retrieval-mcp/target/release/retrieval-mcp \
  --root /absolute/path/to/repo \
  --timeout-seconds 120 --log-file /absolute/path/to/retrieval-events.jsonl
```

Use `/mcp` in Claude Code to check the connection. Add `--ranker semantic --semantic-command '["…/examples/ollama_backend"]'` to put embeddings behind `search_concept`, or `--tools search_exact,read_source` for the grep-and-read baseline. Claude options belong before the server name; server arguments follow `--`. See [Claude Code's MCP documentation](https://code.claude.com/docs/en/mcp).

## Connect Codex

```sh
codex mcp add retrieval -- \
  /absolute/path/to/retrieval-mcp/target/release/retrieval-mcp \
  --root /absolute/path/to/repo \
  --timeout-seconds 120 --log-file /absolute/path/to/retrieval-events.jsonl
```

Alternatively, add this to your Codex configuration, replacing the paths:

```toml
[mcp_servers.retrieval]
command = "/absolute/path/to/retrieval-mcp/target/release/retrieval-mcp"
args = ["--root", "/absolute/path/to/repo", "--timeout-seconds", "120", "--log-file", "/absolute/path/to/retrieval-events.jsonl"]
tool_timeout_sec = 150
```

Use `codex mcp list` to inspect configuration, then start a new session. The examples follow the [official Codex MCP documentation](https://developers.openai.com/codex/mcp). Client setup is documented; the project tests the wire protocol without modifying your agent configuration or running paid model sessions.

## Tools

All tool inputs reject unknown fields. Results include both MCP `structuredContent` and a JSON text equivalent for compatibility, plus output schemas. Line numbers are 1-based and ranges inclusive. Search pages default to 20 results, allow 1–100, and use `offset` / `next_offset`. There is no expensive total-count promise.

The four tools a default session exposes are `search_exact`, `read_source`, `find_callers` and `search_concept`. The catalogue below is all seven; `inspect_symbol`, `find_symbol` and `trace_dependencies` are available only when named, and why they are not default is in [Research options](#research-options--not-needed-to-use-the-server).

| Tool | Use | Example arguments |
|---|---|---|
| `search_exact` | Known text, identifiers, errors, or regex patterns | `{"query":"timeout","path":"src","limit":10}` |
| `read_source` | Inspect or verify a known source location | `{"path":"src/main.rs","start_line":1,"end_line":50}` |
| `inspect_symbol` | Both sides of one symbol at a single hop, before choosing a direction | `{"name":"resolve"}` |
| `find_symbol` | Locate exact-name declarations in Rust/Python/TypeScript | `{"name":"Workspace","limit":10}` |
| `find_callers` | Find direct calls or possible references | `{"name":"resolve","include_references":true,"limit":10}` |
| `trace_dependencies` | Multi-hop callers, callees, or impact | `{"name":"resolve","direction":"callers","depth":3}` |
| `search_concept` | Find behavior when the spelling is unknown | `{"query":"prevent reading files outside the repository","limit":5}` |

`search_exact` is case-sensitive literal search unless `regex:true` or `case_sensitive:false` is supplied. Results represent matching lines, not individual occurrences; excerpts are centered near the first match. Matches remain in stable path/line order for an unchanged repository. It respects ripgrep ignore rules and skips hidden files during traversal; a `path` argument narrows that same traversal with a glob rather than starting a new walk, so scoping can never reach files the unscoped walk would prune. Every page reports `files_searched`: zero over a nonempty repository means ignore rules or the scope emptied the corpus, not that the text is absent. It disables ripgrep config files and excludes `.git` and `target` trees during traversal.

`read_source` defaults to 100 lines, allows at most 500 per request, and returns `next_line` when more source remains. The response has a byte budget as well as a line budget. Long individual lines produce an actionable error; use exact search for an excerpt. Reads can explicitly access ignored or hidden regular files within the root.

`inspect_symbol` answers the orientation question the directed tools make the model guess. One call returns the symbol's definitions, its callers, its callees, and its members when it is a container, each row labelled with its `relation`, plus `counts` that stay complete when rows are capped. Caps are structural: 3 definitions, 6 per direction, 8 members, so a response is a few hundred bytes rather than a subgraph. `has_more` is set whenever any count exceeds its cap, and an unindexed name returns `symbol_status:"unknown_symbol"` with `nearest_indexed_names` instead of a silent empty page. Use it first for relationship questions, then expand one side with `find_callers` or `trace_dependencies`. In the decision study it cut wrong-direction traversals from 15 to 4 of 44; see [experiments/README.md](experiments/README.md).

`find_symbol.path` restricts definitions. `find_callers.path` restricts **call sites**, not target definitions. Caller records include the enclosing symbol, expression, candidate definitions, candidate count, resolution label, confidence explanation, and snippet. At most five candidate definitions accompany each reference; the full count and truncation flag preserve ambiguity. Imports and possible file relationships are bounded context for the returned call-site files.

`trace_dependencies` answers the transitive questions `find_callers` cannot: call chains, dependencies, and impact sets. `direction:"callers"` walks inbound call syntax toward the root; `direction:"callees"` walks outbound from it. Depth defaults to 3 hops and is capped at 5. Each edge names the enclosing caller, the callee name, the call site, the hop distance, and the same low-confidence explanation used elsewhere. Traversal expands each symbol name once, so recursive and mutually recursive code terminates instead of looping. Root definitions accompany the edges, capped at five with a truncation flag. Because names are unqualified, distinct namesakes merge into one traversal node; verify material edges with `read_source`.

## Structural indexing and its limits

The first structural invocation builds a full in-memory snapshot using `rg --files` and the Rust, Python, and TypeScript/TSX Tree-sitter grammars. A definition's concept chunk also includes the contiguous comment and attribute block directly above it, since documentation carries the vocabulary questions are asked in; that one change nearly doubled offline ranking MRR on the authored coreutils suite, and it is the only one of three candidate index changes that survived measurement (see [What the experiments found](#what-the-experiments-found)). Subsequent structural calls reuse the snapshot. No background watcher, incremental update, database, or persistent index is required. Restart the server to rebuild after edits. Exact search and source reads always query current files.

The index records definitions, imports, function/method call syntax, and optional possible identifier references. Comments and string contents are excluded from reference extraction. Rust `mod` and simple Python imports can produce local-module candidates; Rust `use`, aliases, relative Python imports, re-exports, and unusual layouts can remain unresolved.

Resolution is intentionally conservative: same-language spelling matches produce **candidates**, even if only one definition has that name. Receiver types, scopes/shadowing, package/module lookup, macro expansion, conditional compilation, foreign-language bindings, and runtime dispatch are not resolved. Optional references may include variable bindings and other non-call identifiers. Complex callee expressions can be missed. A name with no matching definition remains `unresolved`; multiple namesakes remain `ambiguous`. No candidate is promoted to a confirmed edge.

Every structural result includes `coverage.complete:false`, supported languages, indexed/eligible/unsupported/skipped file counts, bounded skip examples, parse-error count, snapshot timestamp, and freshness/limitation notes. Counts cover files enumerated by ripgrep; hidden files are not counted, and ignored files only under `--no-ignore`. Index-backed `search_concept` responses carry the same `indexed_files` count, so an empty page over an empty corpus says so. A partially parsed file may contribute results but increments the parse-error count. Empty results never establish that a symbol or caller does not exist.

`coverage.budget_truncated` is the one that changes how an answer should be written. `complete:false` is always true — syntactic resolution is never complete — so on its own it cannot distinguish "matching is approximate" from "the index stopped before scanning the repository". When `budget_truncated` is true, whole files were never read, `indexed_files` against `eligible_files` says how many, the limitations string states that absence is not evidence of absence, and the routing instructions tell the model not to answer an exhaustive question from that snapshot as though absence were proven.

Budget checks stop adding files after 20,000 indexed files, 128 MiB of source, 2,000,000 records, or the configured indexing time — sized so `--timeout-seconds` is normally what binds. Measured cost is roughly 3 ms and 150 KB resident per indexed file: Django 5.1.4 indexes 2,786 files in 6 s and about 0.35 GB, VS Code 1.96 indexes 5,188 in 18 s and about 0.8 GB, both fully covered. Per-file limits are 2 MiB, 100,000 named nodes, and syntax depth 128. File-granular aggregate budgets can overshoot by one file. Skips are reported. A repository large enough to truncate says so; narrow `--root`, raise `--timeout-seconds`, or replace the index behind `StructuralBackend`.

## Concept search: one tool, three rankers

`search_concept` is one tool whose ranking mechanism is an operator setting, never a model choice. `--ranker` selects it:

| Ranker | Implementation | Dependencies | Default |
|---|---|---|---|
| `lexical` | In-process BM25 over definition-shaped chunks | none | yes |
| `semantic` | The configured subprocess backend | an adapter, e.g. Ollama | no |
| `hybrid` | Reciprocal rank fusion (k=60) of both | an adapter | no |

The lexical ranker builds documents from a symbol's path, container, name, split identifier, the comment block directly above the definition, and its body, with standard BM25 (k1 1.2, b 0.75) and scope filtering. It needs no model, service, weights, or fetch script, which is why the default configuration is fully offline. Rows name the enclosing definition with caller and callee counts; ask for `fields:["excerpt"]` only when source text is actually needed.

Choosing `semantic` or `hybrid` without `--semantic-command` is a configuration error rather than a silent downgrade. Setting either up is in [Research options](#research-options--not-needed-to-use-the-server); the default needs none of it. Which ranker to prefer is an empirical question this repository measures rather than assumes; see [What the experiments found](#what-the-experiments-found).

## Repository boundaries and response limits

All source paths must be relative to a canonical configured directory. Parent traversal, absolute paths, and symlinks in any path component are rejected. Reads require regular UTF-8 files without NUL bytes and cap input at 2 MiB. This protects ordinary local repository use; path validation and subsequent opens are not an OS capability sandbox against a malicious concurrent filesystem replacement. Use an OS sandbox/read-only checkout for adversarial repositories. Source text is untrusted evidence, never an instruction to the agent.

Structured retrieval payloads are capped at 64 KiB. MCP's text compatibility copy means wire responses can be larger; logs separately record payload bytes and serialized MCP result bytes. Requests reaching a tool handler are capped at 16 KiB; the SDK handles transport framing before that check. Overbroad regexes or huge result sets can hit the bounded capture limit before pagination; narrow the query/path. Invalid arguments and expected backend failures return `isError:true`, and every invocation reaching the handler is logged, including disabled tools and schema failures. Malformed JSON-RPC rejected by the SDK is not a tool invocation.

## Logs

Server `instructions` carry an explicit routing table: literals to `search_exact`, unknown behavior to `search_concept`, declarations to `find_symbol`, relationships to `inspect_symbol` before a directed tool, direct callers to `find_callers`, transitive relationships to `trace_dependencies`, and mixed questions to conceptual discovery followed by structural lookup and `read_source` verification. Tool descriptions repeat the boundary and name the tool to prefer instead. This is routing guidance, not enforcement: no tool is required, blocked, or substituted, and the tool set still controls availability.

Invocation JSONL has `schema_version:1`, an event (`tool_start` / `tool_end`), epoch-millisecond timestamp, session ID, optional operator run ID, MCP request ID, per-session start sequence, the tool set (the `profile` field, holding a profile letter or the `+`-joined tool names), tool name, and argument object. End events add latency, result count, retrieval bytes, MCP response bytes, errors, returned locations, structural coverage, and semantic backend name. Latency includes first-call indexing. Interrupted handlers emit an end event with a cancellation marker; abrupt process termination can leave an unmatched start. Trace diagnostics share stderr but not `--log-file`.

Logs deliberately omit response code bodies, but arguments can still contain sensitive search strings or paths. Keep experiment logs private. They are append-only and flushed through ordinary file writes, without rotation or crash-durable `fsync`. File-write failures are reported on stderr while retrieval continues. Use a separate file per run/process when collecting experiments.

The [study harness](experiments/README.md) drives real agent clients — Claude Code, Codex CLI, or OpenCode — over matched questions and a byte-identical corpus copy per arm, keeping model transcripts beside the server's own JSONL. `end_to_end.py` then scores context consumption offline and identically for every arm, including arms that use no MCP server at all. Answers are graded twice: a strict envelope check, and a resolver that accepts any spelling naming exactly one indexed definition. Routing quality and causal claims still require reading the transcripts.

## Code layout

```text
src/            the server: config, tools, search (lexical/BM25/semantic), index, source, logging
examples/       ollama_backend.rs, the reference semantic adapter, plus its cache support
tests/          mcp_stdio.rs, real JSON-RPC subprocess tests over the wire protocol
experiments/    the study harness: runners, graders, analyzers, question sets, and their tests
```

`src/main.rs` owns startup and stdio. `src/tools` owns MCP schemas and dispatch. `src/search` contains the lexical and semantic interfaces/adapters and bounded subprocess execution. `src/index` owns typed structural results and the Tree-sitter snapshot. `src/source` centralizes paths and bounded reads. `src/logging` owns the versioned event format and append sink. `src/config` owns operator settings.

`LexicalBackend`, `SemanticBackend`, and `StructuralBackend` are the replacement boundaries. Their result types, rather than ripgrep JSON, parser nodes, or embedding vectors, reach MCP. The structural implementation currently owns in-memory storage; a persistent implementation can replace it behind the same interface. The invocation log sink can be changed within `src/logging` without changing tools. There is no speculative storage framework or database migration layer.

Everything under `experiments/` is Python 3.11+ standard library only. `comparison_runner.py` prepares and runs multi-arm agent comparisons; `comparison_gate.py` meters one shared call and byte budget across a trial's MCP upstreams; `codex_wrapper.py` and `opencode_wrapper.py` drive the two supported command-client agents, and `--client claude` drives Claude Code directly; `validate_suite.py` compiles a question suite against the corpus, `tool_reachability.py` proves a supported tool path reaches each gold, and `lexical_oracle.py` rejects questions a bounded search-and-read crawl dissolves; `quality_pass.py` resolves an answer's spelling against definitions found by ripgrep; `end_to_end.py` scores context consumption for OpenCode, Codex and Claude streams alike; `closure_audit.py` classifies every loss a stopping policy causes; `schema_ablation.py` prices each tool's schema against its observed utility; and `study_a*.py` run the offline retrieval and navigation studies. Every runner that can spend money requires `--allow-model-usage` and refuses to overwrite an existing output.

Corpora, run artifacts, transcripts, and model answers are deliberately absent. Trial directories hold full prompts, model reasoning, and verbatim source excerpts from whatever corpus was under test, so they stay local. A published result should ship the pinned question sets, gold answers, and analysis JSON, plus a script that clones the pinned upstream revision — not the corpus copy.

## Testing

```sh
cargo test --locked --all-targets            # 22 library, 7 stdio (1 ignored), 6 example tests
cargo clippy --locked --all-targets -- -D warnings
python3 -W error::ResourceWarning -m unittest discover -s experiments -p 'test_*.py'   # 152 tests
```

All of these run offline and call no model. The Python suite exercises the harness itself: real MCP
transport against a fixture server, the budget gate, the provider-failure abort, the answer
resolver, and every question set's internal consistency. The one test that needs a live Ollama is
ignored by default; run it with `cargo test --locked --test mcp_stdio real_ollama_semantic_over_stdio -- --ignored`.

## Research options — not needed to use the server

Everything above is the default surface: four tools, an in-process lexical ranker, no configuration.
This section is the rest of the instrument. It exists because the default was **chosen by
measurement**, and the measurements have to stay runnable: `--tools` is how any arm of an ablation is
built, the profile letters replay the original availability study, and the semantic backend is the
subject of a published finding rather than a recommendation. None of it is required to run the
server, and none of it costs a default session anything — an un-exposed tool is absent from
`tools/list` and its schema is never sent.

### Restricting the tool set

The server can expose seven tools, and defaults to the four that repaid their schema cost in the tool trials: `search_exact`, `read_source`, `find_callers`, `search_concept`. `inspect_symbol`, `find_symbol` and `trace_dependencies` are un-defaulted, not removed — name them in `--tools` to get them back, singly or together:

```sh
# The full seven-tool surface, named rather than presumed.
./target/release/retrieval-mcp --root /path/to/repo \
  --tools search_exact,read_source,inspect_symbol,find_symbol,find_callers,trace_dependencies,search_concept
```

`--tools` names exactly which ones a session may use, which is how an ablation is run:

```sh
# Is inspect_symbol earning its place? Remove it and change nothing else.
./target/release/retrieval-mcp --root /path/to/repo \
  --tools search_exact,read_source,find_symbol,find_callers,trace_dependencies
```

A tool that is not listed is absent from `tools/list` and refused if called by name. Descriptions and schemas are identical however a tool was enabled, and no failure silently falls back to another tool. Unknown names are rejected at startup rather than ignored.

`--profile A|B|C|D` is retained **only** so commands recorded by the original availability study replay unchanged. It is not a setting for new work; use `--tools`.

| Profile | Equivalent `--tools` |
|---|---|
| A | `search_exact,read_source` |
| B | `search_exact,read_source,inspect_symbol,find_symbol,find_callers,trace_dependencies` |
| C | `search_exact,read_source,search_concept` |
| D | all seven |

The two switches are mutually exclusive: passing both is an error, because the invocation log records one name for the tool set. The letters describe an availability experiment that is finished; every study since has named tools directly, and the tool trials that set the current default ran per-tool arms rather than profiles.

### Semantic backend protocol

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

The adapter enumerates non-hidden, non-ignored source files with extensions `rs`, `py`, `js`, `jsx`, `ts`, `tsx`, `go`, `c`, `h`, `cpp`, and `java`. It creates overlapping 32-line chunks with a 24-line stride, shrinking a window when it would exceed the per-chunk byte budget, and ranks cosine similarity. It reports skipped files. Document vectors persist across subprocess exits: unchanged path/content chunks reuse embeddings, changed or added chunks are embedded, and deleted chunks are removed. Every request still scans current source and embeds its query.

By default the index lives at `<repository>/.retrieval-mcp/semantic.json`, with its vectors in `semantic.vec` beside it. Set `RETRIEVAL_SEMANTIC_CACHE_DIR` to an absolute directory outside the repository for a read-only checkout. The directory contains one repository/model snapshot at a time; use distinct directories for independent repositories or embedding models. Cache identity includes the canonical repository, endpoint, model name, Ollama model digest, and chunker version. Changing identity invalidates reuse. The model digest is checked around document embedding to avoid publishing mixed-model vectors.

The index stores exact chunk text keys and vectors, so treat it as source code. The manifest holds cache identity, entry count, dimensions and vector length; `semantic.vec` holds each key and its little-endian f64 vector, so a warm start reads bytes instead of parsing millions of JSON numbers. New snapshot files use temporary files with private permissions, `sync_all`, and atomic replacement; vectors are published before the manifest that counts them, and a manifest whose vector file is missing, short or long is rejected rather than partly loaded. An OS file lock serializes access to each directory (30-second lock wait); process exit releases it. Failed document embedding leaves the previous snapshot intact. Corrupt caches are reported and preserved: move `semantic.json` and `semantic.vec` aside to rebuild. Caches written in the earlier all-JSON format are reported the same way. Cache files and the default repository cache directory reject symlinks. Directory-entry durability across power loss is not guaranteed.

The implementation intentionally keeps linear cosine ranking and a 20,000-chunk ceiling, at most 2,000 bytes per chunk and 512 MiB per snapshot. It requests no silent model truncation: an input beyond model context fails explicitly. No model download occurs automatically. For larger corpora, replace the backend with an ANN/vector database. `index_note` reports reused/new embedding counts and the model digest in both tool results and invocation logs.

To exercise real embeddings through MCP (opt-in so ordinary tests stay offline):

```sh
cargo build --locked --example ollama_backend
cargo test --locked --test mcp_stdio real_ollama_semantic_over_stdio -- --ignored --nocapture
```
