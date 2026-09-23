# retrieval-mcp

**Code retrieval for coding agents, with the tool surface chosen by measurement instead of taste.**
Four tools over stdio, for Claude Code, Codex, or anything else that speaks MCP. No index to build,
no database, no daemon, no API key, no embedding service unless you want one.

**What you get, in one paragraph.** Your agent answers the same repository questions it already
answers, using 34–42% fewer input tokens and 22–47% fewer tool calls depending on the corpus, and
it reaches its first piece of real evidence sooner. It does **not** answer better: across 468
scored trials on two corpora, answer quality has never separated from your client's own
grep-and-read in either direction, and in one of the two studies the native tools were one answer
ahead. Install it to spend less context per answer, not to get better answers.
[The numbers](#the-result), and the boundary they hold inside.

## Contents

[What it is](#what-it-is) · [The result](#the-result) · [Install](#install) ·
[Quickstart](#quickstart) · [Connect a client](#connect-a-client) · [Tools](#tools) ·
[Limits that change your answer](#limits-that-change-your-answer) ·
[Troubleshooting](#troubleshooting) ·
[Platforms, updating, and removal](#platforms-updating-and-removal) ·
[Build, test, and code layout](#build-test-and-code-layout) ·
[Research options](#research-options--not-needed-to-use-the-server)

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
        ├── find_symbol        → Tree-sitter parse (Rust, Python, JS/TS, Go, Java, C/C++)
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
| Tool calls | 146 | 78 | **77** |
| Persistent context (tok·turns) | 175 k | 227 k | **150 k** |
| Calls to first evidence | 1.79 | 1.35 | **1.17** |
| Answered without evidence | **0** | **0** | **0** |

*Input tokens* counts uncached input plus cache creation — the bytes a turn pays for that were not
already resident. Count cache reads as well and the totals are 2.17 M / 1.83 M / 1.39 M, the same
comparison at −36.2% against native and −24.2% against zvec-grep. Both definitions appear in this
project; the table says which one it uses so no one has to reverse-engineer it.

Against the native control specifically: **−33.5% input tokens, −47% tool calls, −14.5% carried
context, one more correct answer.** Against zvec-grep: −24% input tokens, −34% context, the same
number of calls, the same quality.

**Quality is a tie with zvec-grep and is reported as one** — the arms are discordant on one question
each way — and the context saving is the result: equal quality, fewer turns, less carried context.
Every number came from one model, and none of the findings in this project transferred cleanly
between models, so that is the claim's boundary.
[Paired analysis, the post-hoc sensitivity row, and what the set cost to seal](experiments/README.md#the-held-out-comparison).

**Replicated on a second corpus, with a different model.** 30 mixed-shape questions over an etcd
client corpus, three repetitions, 270 trials, `gpt-5.6-luna`: quality 87 / 89 / 89 of 90 for
retrieval-mcp, native and zvec-grep, on 17.8 M input tokens against native's 32.1 M counting cache
reads — **−44.6%**, or −42.4% on the table's narrower definition — with 320 tool calls against 409
and less carried context. That study **missed its registered
quality criterion by one answer and is published as a miss**; across 468 scored trials on the two
corpora quality has never separated in either direction, and the token gap has never failed to
replicate. [Both studies, with their criteria](experiments/README.md#the-rerun-on-head-the-repaired-suite-and-a-miss-by-one-answer).

**And it stops at a scale.** The same two arms, the same client, on Linux 6.12 — 86,602 files, 21
questions, three repetitions, 126 trials: **56 correct against native's 62, and +16.4% input
tokens where the registration asked for a fifth fewer.** All three registered criteria missed, published
here for the same reason the misses above are. The mechanism was measured rather than reasoned
about: attaching the four-tool surface costs zero prompt tokens, about 9% of what ripgrep prints
locally ever reaches the model while an MCP payload is delivered whole and re-sent, and a shell
call on a tree that size chains a mean of 1.95 sub-commands where this server answers one question
per round trip. On a few hundred files, grep is a poor summary of the tree; on the kernel, a
pipeline is a good one.
[The run, its audit, and the four harness defects it exposed](experiments/README.md#the-linux-kernel-where-the-claim-stops).

**Most of that was a defect, and it is fixed.** A nested ladder cut from the same corpus - 928,
5,637, 18,652 and 86,605 files, every answer present in the smallest - showed the payload holding
flat at 73-75 KB while the gold definition was found for 14, 9, 8 and then **4 of 21** questions.
Not the page size: `limit: 100` returned the same four. In 17 of the 21 the answer's own file
never entered the 400-file candidate set, because files were scored by how many query words they
carried and 56,000 of 60,000 Linux files carry at least one. Scoring by rarity instead takes that
to **12 of 21**, and the same kernel plan re-run under the model gives **58 of 63 against 56, the
token gap against native halving from +16.4% to +8.3%**, with every question predicted to recover
recovering. Two of three registered criteria were still missed and are published as misses.
[The ladder, the repair, and the failure it exposed one level down](experiments/README.md#the-ladder-what-size-actually-did-and-what-the-scorer-did).

**Which binary produced these.** The held-out table is the frozen four-tool surface as of
2026-09-12; the etcd replication ran on the binary that became `0.1.6` minus its last four changes.
Nothing in `0.1.6` has an agent-level measurement behind it: the routing filter is −13.3% of
instruction *bytes*, the Go qualified-call fix moves no row on either benchmark corpus because
neither carries a `go.mod`, the optional `--root` path is not exercised by a benchmark that pins
`--root`, and strict `search_concept` arguments cost a recovery turn on about 1.4% of concept calls.
Each is stated that way in [CHANGELOG.md](CHANGELOG.md) rather than folded into the numbers above.

Everything else — why the surface is four tools and not seven, why the ranker is BM25 and not
embeddings, why a stopping rule was worth more than any retrieval change, and the four post-freeze
optimisations that were measured and rejected — is in
[Findings at a glance](experiments/README.md#findings-at-a-glance). The one line worth carrying out
of it: **correct evidence reduces recovery turns.** The largest efficiency win measured after the
freeze came from a caller row that started telling the truth about a call site, not from a smaller
payload, a richer result, or more retrieval machinery.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/abendrothj/retrieval-mcp/main/install.sh | sh
```

That writes exactly one file — `retrieval-mcp` in `$HOME/.local/bin`, or wherever
`RETRIEVAL_MCP_INSTALL_DIR` points — after verifying the release archive against a published
checksum. No shell startup file, no client configuration, nothing launched, nothing in your
repository. With a Rust toolchain, `cargo install retrieval-mcp` builds it instead, and
[`cargo binstall`](https://github.com/cargo-bins/cargo-binstall)` retrieval-mcp` fetches the same
prebuilt archive without compiling.

Then tell your client, from any directory — the entry carries no path:

```sh
claude mcp add --transport stdio --scope user retrieval -- retrieval-mcp   # Claude Code
codex mcp add retrieval -- retrieval-mcp                                   # Codex
```

Start a new session and the four tools are there. Any other MCP client takes the entry directly:

```json
{"mcpServers": {"retrieval": {"command": "retrieval-mcp"}}}
```

That is the installation. [Connect a client](#connect-a-client) has the scopes, the committed
`.mcp.json` form, the operator options, and how a session decides which repository it reads;
[Platforms, updating, and removal](#platforms-updating-and-removal) has the checksum policy, the
manual download, the supported targets, and how to take it back off.

## Quickstart

```sh
# Run it against any checkout, from that checkout, with the installed binary on PATH.
# No --root: with no client roots to ask for, the server reads the directory it was started in.
# This is the call your agent will make, and exactly what it gets back:
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"find_callers","arguments":{"name":"launch_directory"}}}' \
  | retrieval-mcp 2>/dev/null | tail -1
```

Run inside this repository, the `structuredContent` of that reply reads — line, column and excerpt
elided here because they move with every edit above the call site, everything else checked against
a live call by `experiments/check_docs.py`:

```json
{
  "results": [
    {"path": "src/tools/mod.rs", "name": "launch_directory", "caller": "resolve_root"}
  ],
  "resolution": "unique_name_candidate", "candidate_count": 1,
  "orientation": {"symbol": "launch_directory", "returned_relation": "callers",
                  "incoming_callers": 1, "outgoing_callees": 9},
  "symbol_status": "indexed"
}
```

Each fact appears once. `kind` is omitted because a caller page is made of calls, `expression`
because it repeats the name, and `resolution` and `candidate_count` sit on the page because they
concern the one name it is about — a row carries them only where it disagrees. Every field a
client needs is still derivable, and the repetition is what a session pays for again on every
later request.

Each call site with the definition enclosing it, the relationship counted in both directions, and
a coverage block saying how much of the repository the answer read — from one call, against the
files on disk. It is a
smoke check; a wired-up client never needs it. From a clone rather than an install, run `cargo
build --locked --release --bin retrieval-mcp` first and call `./target/release/retrieval-mcp`
instead.

## Connect a client

The two registration commands are in [Install](#install); this section is what they write and what
else can be done with it. Check the connection with `/mcp` in Claude Code, which connects and lists
the tools; `codex mcp list` prints the configured entry without contacting it, so Codex confirms on
its next session. Claude options belong before the server name and server arguments follow `--`.
Codex also accepts the entry directly:

```toml
[mcp_servers.retrieval]
command = "retrieval-mcp"
tool_timeout_sec = 150
```

`--scope user` writes one entry every project sees; `--scope local` confines it to the repository
you ran it in. To give a whole team the server without anyone running a registration command,
commit the same pathless entry as `.mcp.json` at the project root — `claude mcp add --scope
project …` writes it for you, and Claude Code asks each contributor to approve a project-scoped
server once. Use an absolute path to `./target/release/retrieval-mcp` if you built from a clone.

**Which repository a session reads.** MCP servers are declared in configuration and started by the
client on demand — you never launch this yourself, and it exits with the session. The session reads
exactly one repository, and the client decides which: on the first tool call the server asks for
the client's roots and takes the first one, and a client that reports none — Codex 0.154.0 declares
no roots capability, where Claude Code 2.1.261 declares `roots.listChanged` — leaves the directory
the client launched the server in, which both of them measurably set to the project you opened. Two
launch directories are refused by name rather than indexed: your home directory and the filesystem
root. `--root PATH` overrides all of that and is never asked about, which is how a session reads a
repository the client did not open — a vendored tree, a sibling checkout, a corpus under test.
Nothing is written to your repository and no index is built, ahead of time or at all: every
question searches the repository as it is on disk and parses only the files that answer it, so a
file you saved a second ago is already visible and nothing outlives the call.

| Option | Effect |
|---|---|
| `--root <dir>` | Pins the one repository the session reads, canonicalised once, never overridden by the client. Optional: without it the first tool call takes the client's first root, or the directory the client launched the server in when it reports none, and a roots-changed notification makes the next call resolve again. A home directory or filesystem root is refused rather than indexed |
| `--timeout-seconds <n>` | 1–600, default 30; what normally bounds indexing on a large repository |
| `--log-file <path>` | Append invocation events as JSONL; the parent directory must already exist |
| `--run-id <id>` | Operator label recorded in every event, for matching a run to its trials |
| `--no-ignore` | Search and index ignored files too; `.git`, `target` and hidden files stay excluded |
| `--tools <list>` | Restrict the session's tools — see [Research options](#research-options--not-needed-to-use-the-server) |
| `--ranker <lexical\|semantic\|hybrid>` | Ranking behind `search_concept`; the last two need `--semantic-command` |

The server waits for an MCP client on stdin; it is not an interactive terminal application. Stdout
carries MCP messages only, and logs go to stderr. Concurrent calls are answered independently:
requests are handled as they arrive, each answering its own question against the same corpus, and
every accepted request is answered even if the client closes stdin first — pinned by four stdio
tests that pipeline 24 calls without awaiting replies, issue eight structural calls at once, cancel
a call mid-flight, and close stdin with nine requests in flight. Client setup follows the official
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
`search_exact` pages default to 20 results and `search_concept` to 10; both allow 1–100 and use
`offset` / `next_offset`. There is no expensive total-count promise.

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

**Every answer says how much of the repository it read.** `coverage.complete` is always false —
syntactic resolution is never complete — so the counts are what matter: `indexed_files` is how many
files this answer parsed, `eligible_files` how many source files the repository holds, and
`budget_truncated` says that even this answer's own matches were more than one call could parse, in
which case its absence proves nothing and a narrower `path` should be asked. A caller question
typically reads one to thirty files of a sixty-thousand-file tree and covers all of it for that
name. Every structural result also carries the supported languages, skipped counts and examples,
parse-error count and a freshness note; `search_concept` reports the files its ranking read.

**What is parsed.** Each question searches the repository with ripgrep's `ignore` crate — the same
walk `search_exact` uses, so both describe one corpus — and parses what it found with the Rust,
Python, TypeScript/TSX, Go, Java, C and C++ Tree-sitter grammars. A caller or symbol question
searches for the name; a description searches for its own words and parses the 400 files carrying
most of them; `read_source` annotations parse the one file being labelled. JavaScript and TypeScript are one family: `.js`, `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx`,
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

**Per-answer budgets.** One answer stops parsing after 20,000 files, 128 MiB of source, 2,000,000
records, or the configured timeout, and says so through `budget_truncated`. Per-file limits are
2 MiB, 100,000 named nodes and syntax depth 128; file-granular aggregate budgets can overshoot by
one file, and skips are reported. Files are read and parsed across every available core
(`std::thread::available_parallelism`) and merged in path order, so an answer is exactly what a
single-threaded parse produces and only the wall clock changes. These ceilings almost never bind,
because a question reads the files that answer it: one to thirty for a caller question, 400 for a
ranking, one for a source annotation.

Measured on a 14-core M4 Pro, fresh process, session totals for four structural calls: cobra 0.3 s
and 30 MB, the Django held-out corpus 0.4 s and 68 MB, redis 1.7 s and 223 MB, Django 5.1.4 2.8 s
and 189 MB, VS Code 1.96 2.1 s and 220 MB, and Linux 6.12 — 60,283 eligible files and 1.3 GB of C
— 1.5 s and 65 MB per caller question, 3.3–7.7 s and 566 MB for a ranking. There is no
repository this server refuses to answer over, and no size at which it answers from a stale or
partial index.

## Troubleshooting

Every failure below is one the server states rather than hides; the fix is what to do about it.

| What you see | What it means | What to do |
|---|---|---|
| `/mcp` reports the server failed to connect | the client cannot execute `retrieval-mcp` | check `retrieval-mcp --version` in the same shell; if that fails, `$HOME/.local/bin` is not on your `PATH`, so register the absolute path instead |
| The tools are absent in a session you just configured | client entries load at session start | start a new session; `claude mcp get retrieval` or `codex mcp list` shows what was written |
| `launched this server in your home directory` | the client started the server somewhere that is not a repository, and a home directory is not a corpus anyone meant to index | pass `--root /path/to/repo` in the entry |
| `this client declares roots but roots/list failed` | the client advertised roots and then refused to list them | pass `--root /path/to/repo`; the flag is never overridden |
| `coverage.budget_truncated: true` | this answer's own matches were more than one call could parse, so its absence proves nothing | compare `indexed_files` with `eligible_files`, then narrow `path`, or raise `--timeout-seconds` |
| `symbol_status: "unknown_symbol"` | no file in the repository defines that name, so its empty page is not evidence of a relationship | retry with one of the returned `nearest_indexed_names`, or `search_exact` |
| `files_searched: 0` over a repository with files | ignore rules or the `path` scope emptied the corpus | widen `path`, or run with `--no-ignore` if the code under study is gitignored |
| `response exceeds 64 KiB` | a single row is too large to return | lower `limit`, or narrow `path`; ordinary oversized pages are trimmed and paged instead |
| `the semantic ranker needs a backend` | `--ranker semantic` or `hybrid` without `--semantic-command` | supply the adapter command, or stay on the default `lexical` ranker |

Callers or definitions that you know exist but are missing from a result are usually not a bug:
resolution is deliberately conservative, and [Limits that change your
answer](#limits-that-change-your-answer) says exactly what is and is not resolved.

## Platforms, updating, and removal

Prebuilt binaries cover macOS and Linux on x86-64 and arm64, with a static musl build for Linux
so the server runs in Alpine and distroless containers. **Windows is not supported**: the server
has Unix-only paths — log files created 0600, process-group handling in the tests — that have
never been exercised there, and an untested binary is not a claim this project makes. Under WSL
the Linux binary works, and `cargo install retrieval-mcp` builds from source wherever the
Tree-sitter grammars build.

**What the installer verifies.** It fetches a published checksum — the release's aggregate
`SHA256SUMS`, published from v0.1.5, or the per-archive `<archive>.tar.gz.sha256` that every
release carries — checks the archive for your platform against the single record naming it, and
refuses to extract on a mismatch, a missing record or a duplicate one. `RETRIEVAL_MCP_VERSION=v0.1.6`
installs a specific tag; the default is the latest release. Reading the script first is a
reasonable position, but download it somewhere other than the repository you are about to register
it in:

```sh
curl -fsSL https://raw.githubusercontent.com/abendrothj/retrieval-mcp/main/install.sh \
  -o /tmp/retrieval-mcp-install.sh
sh /tmp/retrieval-mcp-install.sh
```

It registers nothing unless asked. `RETRIEVAL_MCP_REGISTER=claude,codex` runs each client's own
`mcp add` after installing — never an edit to their configuration files, and never a prompt,
because a piped installer's stdin is the script itself and a prompt would silently consume it.
`RETRIEVAL_MCP_SCOPE` picks `user` (one entry every project sees, the default) or `local` (this
repository only, refused outside a git work tree). The entry it writes names
`$HOME/.local/bin/retrieval-mcp`, the absolute path of the binary it just installed, so a
registration done for you does not depend on that directory being on your `PATH`.

**Manual download.** Tarballs are on
[the releases page](https://github.com/abendrothj/retrieval-mcp/releases), each with a `.sha256`
beside it and, from v0.1.5, all of them together in one `SHA256SUMS`. Verify against that manifest
by hand before extracting, with both files in the same directory:

```sh
archive=retrieval-mcp-v0.1.6-aarch64-apple-darwin.tar.gz
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

**Update and removal.** Re-run the installer — it verifies the new archive the same way and
replaces the binary in place — or `cargo install --force retrieval-mcp`, or `cargo binstall
retrieval-mcp`. Client entries name the binary rather than a version, so nothing needs
re-registering; `retrieval-mcp --version` reports which build a client is launching. Removal is two
commands:

```sh
rm ~/.local/bin/retrieval-mcp   # or: cargo uninstall retrieval-mcp
claude mcp remove retrieval     # or: codex mcp remove retrieval
```

That is everything it put on your machine: no configuration edited that you did not ask it to
delete, no file of any kind left in the repositories you queried. The single exception is opt-in and
named as such — the optional Ollama adapter caches embeddings in `<repository>/.retrieval-mcp/`
unless `RETRIEVAL_SEMANTIC_CACHE_DIR` sent them elsewhere, as
[its documentation](examples/OLLAMA_BACKEND.md) says.

## Build, test, and code layout

Building requires Rust 1.90+ (tested with 1.96) and a C compiler for Tree-sitter. The binary needs
nothing at runtime: ripgrep's `grep-searcher`, `grep-regex` and `ignore` crates are linked in, so
there is no `rg` subprocess and no PATH dependency. The SDK is
[`rmcp`](https://github.com/modelcontextprotocol/rust-sdk), the official Tokio-based Rust SDK —
`Cargo.toml` requires 3.2.0, `Cargo.lock` pins the 3.3.0 it resolves to along with the rest of the
working dependency set — and the integration tests negotiate MCP `2025-11-25` over real JSON-RPC
subprocess calls.

```sh
cargo build --locked --release --bin retrieval-mcp
cargo test --locked --all-targets            # 45 library, 22 stdio (1 ignored), 6 example tests
cargo clippy --locked --all-targets -- -D warnings
python3 -W error::ResourceWarning -m unittest discover -s experiments -p 'test_*.py'   # 325 tests
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
results and the Tree-sitter parse, `src/source` paths and bounded reads, `src/logging` the versioned
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
  --tools search_exact,read_source,find_symbol,find_callers,trace_dependencies,search_concept

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
timeouts default to 30 seconds and range from 1 to 600. Backend stdout is capped at 1 MiB — the
lexical ranker runs in process and starts nothing — and stderr is capped at 64 KiB and not
forwarded into tool results, while errors direct
the operator to run the adapter itself for diagnostics. Direct child processes are killed on timeout
or cancellation; adapters are responsible for any descendants they spawn.

[`examples/ollama_backend.rs`](examples/ollama_backend.rs) is the reference implementation, a local
Ollama embedding adapter with a persistent vector cache. Its model settings, chunking, cache
identity, file format and durability guarantees are documented in
[`examples/OLLAMA_BACKEND.md`](examples/OLLAMA_BACKEND.md).
