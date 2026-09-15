# The Ollama semantic adapter

`examples/ollama_backend.rs` is the reference implementation of the
[semantic backend protocol](../README.md#semantic-backend-protocol): a standalone executable the
server launches per request, embedding chunks through a local
[Ollama embedding API](https://docs.ollama.com/api/embed) via `curl`. It is optional in every sense
— the default ranker is in-process BM25 and needs none of this — and it is a *reference*, not a
recommendation: whether embeddings help is the empirical question
[the study measures](../experiments/README.md#findings-at-a-glance), and on the questions measured
here they did not reach the agent.

```sh
ollama pull nomic-embed-text                                   # once, if the model is absent
cargo build --locked --release --example ollama_backend
./target/release/retrieval-mcp --root /absolute/path/to/repo --ranker semantic \
  --semantic-command '["/absolute/path/to/target/release/examples/ollama_backend"]' \
  --timeout-seconds 120
```

## Model and endpoint

Defaults are model `nomic-embed-text` and URL `http://127.0.0.1:11434`, overridden with
`RETRIEVAL_EMBED_MODEL` and `RETRIEVAL_OLLAMA_URL` in the *server's* environment, since the server
is what launches the adapter. Nomic models receive the documented
[query/document prefixes](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5); other models
receive plain text. Use the same model for query and document vectors. No model is downloaded
automatically.

## What it indexes

The adapter enumerates non-hidden, non-ignored source files with extensions `rs`, `py`, `js`, `jsx`,
`ts`, `tsx`, `go`, `c`, `h`, `cpp` and `java`. It creates overlapping 32-line chunks with a 24-line
stride, shrinking a window that would exceed the per-chunk byte budget, and ranks by cosine
similarity. Skipped files are reported. Ranking stays linear over at most 20,000 chunks, at most
2,000 bytes per chunk and 512 MiB per snapshot; for larger corpora replace the backend with an
ANN or vector database. An input beyond the model's context fails explicitly rather than being
silently truncated.

Every request still scans current source and embeds its query. `index_note` reports reused and new
embedding counts and the model digest, in both tool results and invocation logs.

## The cache, and where it is written

Document vectors persist across subprocess exits: unchanged path/content chunks reuse their
embeddings, changed or added chunks are embedded, deleted chunks are removed.

By default the index lives at `<repository>/.retrieval-mcp/semantic.json`, with its vectors in
`semantic.vec` beside it. **This is the only thing in the project that writes inside a repository**,
which is why it is opt-in and named here: set `RETRIEVAL_SEMANTIC_CACHE_DIR` to an absolute
directory outside the repository for a read-only checkout, or for a corpus under measurement — a
vector cache written inside a corpus under test is
[an entry in the defect ledger](../experiments/README.md#harness-defect-ledger).

One directory holds one repository/model snapshot at a time; use distinct directories for
independent repositories or embedding models. Cache identity includes the canonical repository,
endpoint, model name, Ollama model digest and chunker version, and changing any of them invalidates
reuse. The digest is re-checked around document embedding so mixed-model vectors are never
published.

## Cache format and durability

The index stores exact chunk text keys and vectors, so treat it as source code. The manifest holds
cache identity, entry count, dimensions and vector length; `semantic.vec` holds each key and its
little-endian f64 vector, so a warm start reads bytes instead of parsing millions of JSON numbers.

New snapshot files use temporary files with private permissions, `sync_all` and atomic replacement;
vectors are published before the manifest that counts them, and a manifest whose vector file is
missing, short or long is rejected rather than partly loaded. An OS file lock serializes access to
each directory with a 30-second wait, released on process exit. Failed document embedding leaves the
previous snapshot intact. Corrupt caches — and caches written in the earlier all-JSON format — are
reported and preserved: move `semantic.json` and `semantic.vec` aside to rebuild. Cache files and
the default repository cache directory reject symlinks. Directory-entry durability across power
loss is not guaranteed.

## Exercising it through MCP

Real embeddings over the wire are opt-in, so ordinary tests stay offline:

```sh
cargo build --locked --example ollama_backend
cargo test --locked --test mcp_stdio real_ollama_semantic_over_stdio -- --ignored --nocapture
```

The subprocess runs with the operator's permissions: it is a trusted adapter, not a sandbox. The
server checks the paths a backend returns and reads its own excerpts from current source, but it
cannot stop a separately configured program from reading or transmitting other data.
