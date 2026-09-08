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

## Measured cost (this machine, Ollama `nomic-embed-text`, 2026-09-08)

| Operation | Cost |
|---|---|
| Structural snapshot, first call | 13.6 s (2 ms warm) |
| Semantic index, cold | 263 s for 11,649 chunks |
| Semantic query, warm | 3.3 s per call |
| Semantic cache | 123 MB per cache directory |

Consequences for the run configuration, none of them optional:

1. `--tool-timeout` must exceed both build times; the current default of 10 s fails the first
   structural call, and the server caps the setting at 600 s, only 2.3× the cold semantic build.
2. A fresh per-attempt semantic cache is no longer affordable: 123 MB and 4.4 minutes per trial.
   Semantic conditions need one shared warm cache, pinned and recorded as a warm-start condition.
3. The warm semantic call costs 3.3 s, largely spent parsing the 123 MB JSON cache in a per-call
   adapter process. Latency contrasts involving C or D therefore measure adapter I/O as much as
   retrieval. Either record that explicitly, or move the cache to a binary format before claiming
   any latency result for semantic profiles.

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

Retrieval quality was not otherwise changed: same model, same stride, same cosine ranking.
