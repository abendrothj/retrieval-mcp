# v2 corpus: uutils/coreutils

Pinned at `c29429c13b79b3ab7928c7723eeaf95905a2e35e` (2026-09-08). Working checkout:
`../corpora/coreutils` (shallow clone since 2026-04-01, detached at that commit). Frozen corpus:
`../runs/projects-v2-suite/coreutils/`, exported by `snapshot_projects.py` — committed `.rs`/`.py`
blobs only, per-file SHA-256 in `snapshot.json`, corpus SHA-256 `5c3cc79576f454df…`.

| Property | v1 ModelShare | v2 coreutils |
|---|---:|---:|
| Source files | 17 | 673 (662 Rust, 11 Python) |
| Source lines | 3,671 | 271,217 |
| `fn uumain` definitions | — | 109 |
| `fn new` definitions | — | 162 |
| Lexical hits for `uumain` | — | 398 |
| Test files | 0 | 119 |

Why this corpus: 109 near-identical utilities over a shared `uucore` library make duplicate-name
disambiguation, cross-crate chains and test-only callers ordinary rather than contrived, and every
broad query truncates — `search_exact("fn uumain", limit=100)` still reports `has_more`. 1,474
commits since 2026-05-01 touch 431 of 662 Rust files, so questions can be drawn from code that
post-dates the model's training cutoff, and pre-cutoff questions become a contamination probe.

## Measured cost (this machine, release build, Ollama `nomic-embed-text`, 2026-09-08)

| Operation | Cost |
|---|---|
| `search_exact` | 24 ms |
| Structural snapshot, first call | 2.5 s (under 1 ms warm) |
| Semantic index, cold | ~270 s for 11,649 chunks, embedding-bound |
| Semantic query, warm | 0.24 s (0.51 s for the first call in a process) |
| Semantic cache | 83 MB per cache directory |

Two earlier figures in this file were wrong and are corrected above: a 13.6 s structural build and a
3.3 s warm semantic query came from `target/debug` binaries and a JSON vector cache. The v1 runs used
`target/release`, so their latencies are unaffected. Pin release binaries for every measured run;
a debug server inflates structural indexing about fivefold and semantic queries about sixfold.

Consequences for the run configuration:

1. Cold semantic indexing (~270 s) does not fit the 10 s default `--tool-timeout`, and the server
   caps that setting at 600 s. Structural indexing at 2.5 s now fits comfortably. Warm the semantic
   index before trials rather than paying for it inside one.
2. A fresh per-attempt semantic cache remains unaffordable: 83 MB and about 4.5 minutes per trial.
   Semantic conditions need one shared warm cache, pinned and recorded as a warm-start condition.
3. Semantic calls cost roughly ten times a lexical search (0.24 s versus 24 ms). That gap is now
   query embedding, corpus rescan and the linear scan — real retrieval work — rather than cache
   parsing, so latency contrasts involving C and D are interpretable as long as the warm-start
   condition and the per-call adapter process are recorded with them.

## Adapter changes required to index this corpus

`examples/ollama_backend.rs` and `examples/support/cache.rs` were sized for a ~1,000-chunk toy
repository. Three ceilings blocked this corpus, all raised deliberately:

- `MAX_CHUNKS` 1,000 → 20,000, with the linear-scan and memory ceiling named at the constant.
- `MAX_CACHE_BYTES` 128 MB → 512 MB, sized for JSON f64 vectors at that chunk count.
- New `MAX_CHUNK_BYTES` of 2,000: `nomic-embed-text` returned HTTP 400 for windows of dense table
  source (`src/uu/dd/src/conversion_tables.rs`), and a bisection put its practical limit near 2,804
  bytes on that content. Windows now shrink to the byte budget instead of truncating text, so
  coverage is preserved and overlap shrinks. The chunker identity is now
  `lines-32-stride-24-max2000b-v2`, which invalidates caches built by the old chunker.

A fourth change followed from the timings: `semantic.json` now holds only cache identity, entry
count, dimensions and vector length, and the vectors live beside it in `semantic.vec` as
little-endian f64. Parsing nine million JSON numbers on every call was most of the old warm cost.
Values round-trip exactly, and the ranked hits and scores are identical before and after.

Retrieval quality was not otherwise changed: same model, same stride, same cosine ranking.
