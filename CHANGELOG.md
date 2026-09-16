# Changelog

What shipped in each release of the `retrieval-mcp` binary. The findings behind the design, their
protocols and their caveats are in [experiments/README.md](experiments/README.md); this file records
releases only.

## Unreleased

**`--root` is optional, and the recommended entry no longer carries a path.** Every configuration
entry repeated the repository the client had already opened, as `--root .` resolved against
whatever directory the client launched the server in — a copy of a fact the client holds, and the
reason a user-scope entry was a guess rather than a setting. A session started without `--root` now
resolves one on the first tool call: the client's first root if it reports any, otherwise the
directory the client launched the server in, which is what every other stdio MCP server uses and
the same directory `--root .` always meant. A roots-changed notification makes the next call
resolve again, and the warm snapshot survives when the answer names the same directory. `--root`
still wins, is never asked about and is never overridden, so it remains how an operator points a
session at a repository the client did not open.

Two launch directories are refused by name instead of indexed — a home directory and the filesystem
root — because a corpus of everything is not a repository anyone meant to name, and `install.sh`
already refused to register a local entry outside a work tree for the same reason. The installer's
own registrations and every documented entry dropped `--root .`, and `RETRIEVAL_MCP_SCOPE` now
defaults to `user`: with no path in the entry, one registration reads whichever project is open.

`roots/list` is deprecated by SEP-2577 — advisory, no wire change, functional for at least a year
past the deprecating spec version — so it has exactly one call site in `client_root`, with the
launch directory behind it as the non-deprecated path. Both clients were read off a live handshake
against a server started with no root at all: Claude Code 2.1.261 declares `roots.listChanged` on
protocol `2025-11-25`, Codex 0.154.0 declares `roots: None` on `2025-06-18`, and both spawn the
server with the opened project as its working directory — which is why the fallback, not the
deprecated request, is what makes Codex work without a path. A client that declares roots and then never answers is bounded by `--timeout-seconds` and falls
back to the launch directory, because without a deadline the first tool call never returns and
every later one queues behind it. Six stdio tests cover the resolution chain, including that
silence; no model session was run and no agent-level claim is made for it.

**`search_concept` rejects unknown fields, like every other tool.** Its argument struct was the
one without `deny_unknown_fields`, so a misspelled `limt` was silently ignored and the caller got
a default page it never asked for. The README had claimed the strict behaviour for all seven tools
since the surface froze; the code is now what the sentence said rather than the other way round.
It is not free: across every archived run 14 of 977 `search_concept` calls carried a field the
strict schema rejects - all of them a `project` the model invented, all in trials that answered
correctly anyway - so the measured cost is a recovery turn on about 1.4% of concept calls, against
a silent wrong-scope answer that this project has no way to detect.

**The README has a shape contract, checked by `check_docs.py`.** An audit found it answering "how
do I install this" for 131 lines before answering "what is this", explaining client registration
in eight places — one a byte-identical duplicate — and carrying five claims the code contradicted:
a 4 MiB cap on a ranker that starts no subprocess, a 20-result default that was 10 for
`search_concept`, an SDK version, a checksum policy that disagreed with itself seventy lines
later, and an ablation example that changed two variables while saying it changed one. The claims
are fixed and the structure is now enforced: sections appear once each in a fixed order with proof
before procedure, the install section has a line budget, no fenced block may appear twice, the
client registration commands may appear in exactly one block, and the contents list must name
every section. The quickstart's sample response is compared field by field against a live call, so
the one output the README shows cannot go stale silently.

## 0.1.5

**`cargo binstall retrieval-mcp` works.** The binstall metadata that points at the release archives
landed in the repository after 0.1.4 was already published, and crates.io serves the manifest as it
was at publish time — so the README advertised a command that had no pinned URL behind it. This
release publishes the manifest that carries it.

It is also the first release whose assets include the aggregate `SHA256SUMS` the installer prefers;
the per-archive `<archive>.tar.gz.sha256` files are still published for every target, and the
installer accepts either.

## 0.1.4

**`--version` answers.** The argument parser knew `--help` and treated every other flag as one
needing a value, so `retrieval-mcp --version` replied `missing value for --version` — the first
question a release script, a package manager or a user checking which build their client launched
asks, answered with something nobody can act on. It had been wrong since 0.1.0. `--version` and
`-V` now print `retrieval-mcp <version>` and need no `--root`.

## 0.1.3

**Five more languages, audited rather than announced.** The structural index now parses Go, Java, C,
C++ and the JavaScript half of the ECMAScript family alongside Rust, Python and TypeScript, and
languages are matched as families rather than by file extension — a `.js` call site resolves against
a `.ts` definition, and an ESM specifier written `./util.js` resolves to `util.ts`. Each family was
measured against an independent reading of a real corpus before being advertised: 20 symbols each
from Cobra, Gson, Redis, LevelDB and ESLint, 99 of 100 definitions found, caller precision 0.92–1.00
and recall 0.93–1.00, under a pre-registered pass criterion the first run failed.
[Protocol and artifacts](experiments/README.md#adding-five-languages).

**Two defects the audit found in languages that were already shipped.** `new Table(rows)` was never
a call site in JavaScript or TypeScript, because a constructor invocation is a `new_expression`
rather than a call expression, so asking who constructs a class returned every factory that mentions
it and none of the code that builds it. And a reference-returning C++ accessor —
`const BlockHandle& metaindex_handle() const {` — was no definition at all. Both are fixed and
pinned by tests.

**What it costs.** Four more Tree-sitter grammars make the release binary 15.9 MB instead of
11.3 MB and a clean release build 47 s instead of 37 s on an M4 Pro. A cold Django snapshot goes
3.9 s → 5.9 s, because 112 JavaScript files are now indexed that were not before; four minified
vendor bundles account for about 0.7 s of that. Nothing indexes more slowly per file than it did.

## 0.1.2

Same architecture, materially better implementation: fewer wrong facts, less wasted agent work, no
architectural churn. Two of the changes carry measured deltas, and both are workload-specific
results rather than whole-system performance claims.

**Correcting TypeScript caller attribution.** A call written inside a local binding now resolves to
the enclosing indexed function instead of the non-indexed local variable. In an isolated caller
workload — six questions × three repetitions × two arms, seed, corpus and configuration held
constant, the attribution fix the only difference — this reduced source reads 74% (0.83 → 0.22 per
trial), tool calls 30% (3.67 → 2.56), total model input 39.5% (773,300 → 467,775 tokens) and
worst-trial input 81% (158,376 → 30,098), with answer quality unchanged (15/18 in both arms, graded
credit 0.944, zero answers without evidence).
[Detail](experiments/README.md#layers-5-and-6-isolating-the-feature-from-the-fix).

**Including attached doc comments in concept-search chunks.** This improved offline ranking MRR from
0.126 to 0.234 over 30 authored questions on three corpora cut after the change was written; no
corresponding agent-level quality gain was observed, and none is claimed.
[Detail](experiments/README.md#three-index-changes-one-survivor).

These are measured effects on the workloads named, not a statement that 0.1.2 is 39.5% cheaper than
0.1.1 in general: the release contains several changes, and that figure comes from a workload
deliberately chosen because the defect bit there. The remaining changes — `files_searched`,
`indexed_files`, a 16 MiB file listing, glob-scoped search, `--no-ignore`, line counts in
past-end-of-file errors — are correctness and observability work with no measured effect on cost or
quality.
