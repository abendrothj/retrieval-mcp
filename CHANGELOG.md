# Changelog

What shipped in each release of the `retrieval-mcp` binary. The findings behind the design, their
protocols and their caveats are in [experiments/README.md](experiments/README.md); this file records
releases only.

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
