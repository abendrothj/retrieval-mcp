//! Persistent semantic backend: refresh content, reuse document vectors, embed each query.
//! No embedding or persistence dependency is imposed on the MCP server.
use anyhow::{Context, Result, ensure};
use retrieval_mcp::{
    index::StructuralIndex,
    search::{
        lexical::{pagination, validate_query},
        process,
        semantic::{BackendHit, SemanticRequest, SemanticResponse},
    },
    source::Workspace,
};
use serde::Deserialize;
use serde_json::json;
use std::{io::Read, path::Path, time::Duration};
use tokio::process::Command;

#[path = "support/cache.rs"]
mod cache;

/// Every vector is scanned per query and the whole cache is held in memory, so this ceiling is
/// storage and scan cost, not accuracy: 20000 chunks of 768 f64 is roughly 120 MB resident and a
/// linear scan per query. Above it, use an ANN index instead of raising the number again.
const MAX_CHUNKS: usize = 20000;

/// nomic-embed-text rejects input beyond its context window; on dense table source that limit was
/// reached near 2800 bytes, so windows are capped well below it. A backend that counts tokens for
/// the configured model would not need this margin.
const MAX_CHUNK_BYTES: usize = 2000;

struct Chunk {
    path: String,
    start: usize,
    end: usize,
    text: String,
}

/// Definition spans give a hit a name and a complete body; line windows cut functions in half and
/// merge unrelated neighbours. Tree-sitter covers Rust and Python, so other languages keep windows.
fn symbol_chunks(path: &str, text: &str, lines: &[&str]) -> Option<Vec<Chunk>> {
    if !matches!(
        Path::new(path).extension().and_then(|s| s.to_str()),
        Some("rs" | "py" | "ts" | "tsx")
    ) {
        return None;
    }
    let symbols = StructuralIndex::symbol_spans(path, text, Duration::from_secs(5)).ok()?;
    let mut chunks = Vec::new();
    for symbol in symbols {
        // A container's own chunk would re-embed every method it holds; keep the leaf definitions.
        if matches!(symbol.kind.as_str(), "mod_item" | "impl_item") {
            continue;
        }
        let end = symbol.end_line.min(lines.len());
        if symbol.line > end {
            continue;
        }
        // Doc comments above the definition carry the intent the query is usually phrased in.
        let mut start = symbol.line;
        while start > 1 {
            let previous = lines[start - 2].trim_start();
            if previous.starts_with("///") || previous.starts_with("#[") || previous.starts_with('#')
            {
                start -= 1;
            } else {
                break;
            }
        }
        let header = match &symbol.container {
            Some(owner) => format!("{path} :: {owner} :: {}\n", symbol.name),
            None => format!("{path} :: {}\n", symbol.name),
        };
        let mut body = lines[start - 1..end].join("\n");
        while header.len() + body.len() > MAX_CHUNK_BYTES && body.contains('\n') {
            body.truncate(body.rfind('\n').unwrap_or(0));
        }
        let text = format!("{header}{body}");
        if text.trim().is_empty() || text.len() > MAX_CHUNK_BYTES {
            continue;
        }
        chunks.push(Chunk {
            path: path.to_owned(),
            // The protocol bounds a hit's range at 500 lines, and the server rejects the WHOLE page
            // for one row outside it - measured: two semantic pages in
            // runs/semantic-resistant-20260924 came back as
            // "semantic backend returned an invalid line range", which reads exactly like the
            // ranker finding nothing. A definition longer than that is real (vLLM and prowler both
            // have them), and the body above is already truncated to the byte budget, so the range
            // was claiming more than was ever embedded. Clamping makes it honest and compliant; the
            // row still names the right definition and read_source can fetch the rest.
            start: symbol.line,
            end: end.min(symbol.line + 499),
            text,
        });
    }
    (!chunks.is_empty()).then_some(chunks)
}

fn chunks(workspace: &Workspace, files: Vec<String>) -> Result<(Vec<Chunk>, usize, usize)> {
    let mut chunks = Vec::new();
    let mut skipped = 0;
    let mut unembeddable = 0;
    for path in files {
        if !matches!(
            Path::new(&path).extension().and_then(|s| s.to_str()),
            Some("rs" | "py" | "js" | "jsx" | "ts" | "tsx" | "go" | "c" | "h" | "cpp" | "java")
        ) {
            skipped += 1;
            continue;
        }
        let text = match workspace.text(&path) {
            Ok(text) => text,
            Err(_) => {
                skipped += 1;
                continue;
            }
        };
        let lines: Vec<_> = text.lines().collect();
        // A single line past the chunk budget cannot be embedded, and shrinking the window cannot
        // help once the window is one line. Minified vendor assets are the real case - Django ships
        // `xregexp.min.js` as one 153 KB line - and they are not retrieval targets. Skipping the
        // file and reporting it keeps the design's rule that nothing is silently truncated, while
        // letting the rest of a real repository be indexed: before this, one bundle failed the whole
        // request and the semantic ranker could not run on any corpus containing one.
        if lines.iter().any(|line| line.len() > MAX_CHUNK_BYTES) {
            unembeddable += 1;
            continue;
        }
        if let Some(symbols) = symbol_chunks(&path, &text, &lines) {
            chunks.extend(symbols);
            ensure!(
                chunks.len() <= MAX_CHUNKS,
                "semantic backend supports at most 20000 chunks; configure a larger vector store for this repository"
            );
            continue;
        }
        for start in (0..lines.len()).step_by(24) {
            let mut end = (start + 32).min(lines.len());
            let mut text = lines[start..end].join("\n");
            // The embedding model rejects input past its context, so shrink the window to a byte
            // budget rather than truncating text: the overlap shrinks, coverage does not.
            while text.len() > MAX_CHUNK_BYTES && end > start + 1 {
                end -= 1;
                text = lines[start..end].join("\n");
            }
            if text.trim().is_empty() {
                continue;
            }
            ensure!(
                text.len() <= MAX_CHUNK_BYTES,
                "one line exceeds the embedding chunk budget; use a backend with token-aware chunking"
            );
            chunks.push(Chunk {
                path: path.clone(),
                start: start + 1,
                end,
                text,
            });
            // Linear cosine ranking is limited to MAX_CHUNKS; use an ANN backend above this ceiling.
            ensure!(
                chunks.len() <= MAX_CHUNKS,
                "semantic backend supports at most 20000 chunks; configure a larger vector store for this repository"
            );
        }
    }
    Ok((chunks, skipped, unembeddable))
}

#[derive(Deserialize)]
struct Embeddings {
    embeddings: Vec<Vec<f64>>,
}

async fn embed(texts: &[String], model: &str, endpoint: &str) -> Result<Vec<Vec<f64>>> {
    let mut command = Command::new("curl");
    command.args([
        "--disable",
        "--silent",
        "--show-error",
        "--fail",
        "--max-time",
        "120",
        "--noproxy",
        "*",
        "--header",
        "Content-Type: application/json",
        "--data-binary",
        "@-",
        "--url",
        endpoint,
    ]);
    let input = serde_json::to_vec(&json!({"model": model, "input": texts, "truncate": false}))?;
    let (status, output) = process::run(
        &mut command,
        Some(input),
        Duration::from_secs(125),
        8 * 1024 * 1024,
    )
    .await?;
    ensure!(
        status == 0,
        "Ollama embedding request failed; check that Ollama is running and the embedding model is installed (or chunk exceeds context)"
    );
    let embeddings: Embeddings =
        serde_json::from_slice(&output).context("invalid Ollama embedding response")?;
    ensure!(
        embeddings.embeddings.len() == texts.len(),
        "Ollama returned an incorrect vector count"
    );
    Ok(embeddings.embeddings)
}

fn cosine(a: &[f64], b: &[f64]) -> Result<f64> {
    ensure!(
        !a.is_empty() && a.len() == b.len() && a.iter().chain(b).all(|x| x.is_finite()),
        "invalid embedding dimensions or values"
    );
    let norm =
        a.iter().map(|x| x * x).sum::<f64>().sqrt() * b.iter().map(|x| x * x).sum::<f64>().sqrt();
    ensure!(norm.is_finite() && norm > 0.0, "invalid embedding norm");
    let score = a.iter().zip(b).map(|(x, y)| x * y).sum::<f64>() / norm;
    ensure!(score.is_finite(), "invalid similarity score");
    Ok(score)
}

#[tokio::main]
async fn main() -> Result<()> {
    let mut bytes = Vec::new();
    std::io::stdin()
        .take(16 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= 16 * 1024, "request too large");
    let request: SemanticRequest = serde_json::from_slice(&bytes)?;
    ensure!(
        request.protocol_version == 1,
        "unsupported protocol_version"
    );
    validate_query(&request.query)?;
    pagination(Some(request.limit), Some(request.offset))?;
    let workspace = Workspace::new(Path::new(&request.root))?;
    let (cache_path, _lock) = cache::open(&workspace).await?;
    let mut command = Command::new("rg");
    command.current_dir(workspace.root()).args([
        "--no-config",
        "--files",
        "--null",
        "--sort",
        "path",
        "--glob",
        "!.git/**",
        "--glob",
        "!target/**",
    ]);
    let (status, output) =
        process::run(&mut command, None, Duration::from_secs(30), 2 * 1024 * 1024).await?;
    ensure!(status == 0 || status == 1, "cannot list source files");
    let files = output
        .split(|b| *b == 0)
        .filter(|b| !b.is_empty())
        .map(|b| String::from_utf8(b.to_vec()).context("non-UTF-8 path"))
        .collect::<Result<Vec<_>>>()?;
    let model =
        std::env::var("RETRIEVAL_EMBED_MODEL").unwrap_or_else(|_| "nomic-embed-text".into());
    let host =
        std::env::var("RETRIEVAL_OLLAMA_URL").unwrap_or_else(|_| "http://127.0.0.1:11434".into());
    let endpoint = format!("{}/api/embed", host.trim_end_matches('/'));
    let digest = model_digest(&model, &host).await?;
    // Enumeration and reads happen under the cache lock.
    let (chunks, skipped, unembeddable) = chunks(&workspace, files)?;
    let identity = json!({"version":1,"root":workspace.root(),"model":model,"digest":digest,"endpoint":endpoint,"chunker":"symbols-rs-py-else-lines-32-stride-24-max2000b-v3"});
    let previous = cache::Snapshot::load(&cache_path)?;
    let mut vectors = cache::reuse(previous.as_ref(), &identity, &chunks);
    let missing: Vec<_> = vectors
        .iter()
        .enumerate()
        .filter_map(|(i, v)| v.is_none().then_some(i))
        .collect();
    for batch in missing.chunks(32) {
        let texts: Vec<_> = batch
            .iter()
            .map(|&i| {
                let chunk = &chunks[i];
                if model.starts_with("nomic-embed-text") {
                    format!("search_document: {}\n{}", chunk.path, chunk.text)
                } else {
                    format!("{}\n{}", chunk.path, chunk.text)
                }
            })
            .collect();
        for (&i, vector) in batch.iter().zip(embed(&texts, &model, &endpoint).await?) {
            vectors[i] = Some(vector);
        }
    }
    let vectors: Vec<_> = vectors
        .into_iter()
        .map(|v| v.context("missing document embedding"))
        .collect::<Result<_>>()?;
    ensure!(
        model_digest(&model, &host).await? == digest,
        "embedding model changed during indexing; retry"
    );
    let snapshot = cache::Snapshot::new(identity, &chunks, &vectors)?;
    if previous.as_ref() != Some(&snapshot) {
        snapshot.save(&cache_path)?;
    }
    let query = if model.starts_with("nomic-embed-text") {
        format!("search_query: {}", request.query)
    } else {
        request.query
    };
    let mut ranked = Vec::new();
    if !chunks.is_empty() {
        let query_vector = embed(&[query], &model, &endpoint).await?.remove(0);
        for (chunk, vector) in chunks.iter().zip(&vectors) {
            ranked.push(BackendHit {
                path: chunk.path.clone(),
                start_line: chunk.start,
                end_line: chunk.end,
                score: cosine(&query_vector, vector)?,
            });
        }
    }
    ranked.sort_by(|a, b| {
        b.score
            .total_cmp(&a.score)
            .then(a.path.cmp(&b.path))
            .then(a.start_line.cmp(&b.start_line))
    });
    let has_more = ranked.len() > request.offset + request.limit;
    let response = SemanticResponse {
        protocol_version: 1,
        backend: format!("ollama/{model}"),
        index_note: format!(
            "Persistent index: {} chunks, {} document embeddings reused, {} embedded; {skipped} files skipped, {unembeddable} skipped for a line past the chunk budget. Model digest {digest}. Source scanned this request; cosine ranking.",
            chunks.len(),
            chunks.len() - missing.len(),
            missing.len()
        ),
        results: ranked
            .into_iter()
            .skip(request.offset)
            .take(request.limit)
            .collect(),
        has_more,
    };
    println!("{}", serde_json::to_string(&response)?);
    Ok(())
}

async fn model_digest(model: &str, host: &str) -> Result<String> {
    let mut command = Command::new("curl");
    command.args([
        "--disable",
        "--silent",
        "--show-error",
        "--fail",
        "--max-time",
        "10",
        "--noproxy",
        "*",
        "--url",
        &format!("{}/api/tags", host.trim_end_matches('/')),
    ]);
    let (status, bytes) =
        process::run(&mut command, None, Duration::from_secs(15), 1024 * 1024).await?;
    ensure!(status == 0, "cannot query Ollama model identity");
    let value: serde_json::Value = serde_json::from_slice(&bytes)?;
    let tag = if model.contains(':') {
        model.to_string()
    } else {
        format!("{model}:latest")
    };
    value["models"]
        .as_array()
        .context("invalid Ollama model list")?
        .iter()
        .find(|entry| entry["name"] == tag || entry["model"] == tag)
        .and_then(|entry| entry["digest"].as_str())
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .context("embedding model is not installed or has no digest; install it with ollama pull")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_definition_longer_than_the_protocol_range_is_clamped_not_rejected() {
        // `src/search/semantic.rs` requires end_line - start_line < 500 and rejects the entire
        // page for one row outside it, so an unclamped range silenced the semantic ranker on any
        // corpus holding a 500-line definition - and looked like a ranking failure.
        let root = tempfile::tempdir().unwrap();
        let workspace = Workspace::new(root.path()).unwrap();
        let mut body = String::from("def wide():\n");
        for i in 0..900 {
            body.push_str(&format!("    x{i} = {i}\n"));
        }
        std::fs::write(root.path().join("wide.py"), body).unwrap();
        let (chunks, _, _) = chunks(&workspace, vec!["wide.py".into()]).unwrap();
        assert!(!chunks.is_empty(), "the definition is still indexed");
        for chunk in &chunks {
            assert!(
                chunk.end - chunk.start < 500,
                "chunk {}..{} exceeds the protocol's range bound",
                chunk.start,
                chunk.end
            );
        }
    }

    #[test]
    fn a_line_past_the_chunk_budget_skips_its_file_instead_of_failing_the_request() {
        // Django ships `xregexp.min.js` as one 153 KB line, and before this the whole request
        // failed with "one line exceeds the embedding chunk budget" - so the semantic ranker could
        // not run on any real repository carrying a minified asset. The file is skipped and
        // counted; every other file still indexes.
        let root = tempfile::tempdir().unwrap();
        let workspace = Workspace::new(root.path()).unwrap();
        std::fs::write(root.path().join("min.js"), format!("var a={};\n", "x".repeat(3000))).unwrap();
        std::fs::write(root.path().join("ok.py"), "def wrap(width):\n    return width\n").unwrap();
        let (chunks, skipped, unembeddable) =
            chunks(&workspace, vec!["min.js".into(), "ok.py".into()]).unwrap();
        assert_eq!(unembeddable, 1, "the minified file is reported, not silently dropped");
        assert_eq!(skipped, 0, "and not conflated with an unsupported extension");
        assert!(
            chunks.iter().all(|c| c.path == "ok.py"),
            "every chunk comes from the file that could be embedded"
        );
        assert!(!chunks.is_empty(), "the rest of the repository still indexes");
    }

    #[test]
    fn cosine_and_chunk_ranges() {
        assert!((cosine(&[1.0, 0.0], &[1.0, 0.0]).unwrap() - 1.0).abs() < 1e-12);
        assert!(cosine(&[0.0], &[0.0]).is_err());
        assert!(cosine(&[1.0], &[1.0, 2.0]).is_err());
        let root = tempfile::tempdir().unwrap();
        let workspace = Workspace::new(root.path()).unwrap();
        std::fs::write(
            root.path().join("a.rs"),
            "/// Wrap lines to a width.\nfn wrap(width: usize) {}\nstruct Held;\nimpl Held {\n    fn run(&self) {}\n}\n",
        )
        .unwrap();
        let (symbols, _, _) = chunks(&workspace, vec!["a.rs".into()]).unwrap();
        // One chunk per definition, spanning the whole definition, named and carrying its doc.
        assert_eq!(
            symbols
                .iter()
                .map(|c| (c.start, c.end))
                .collect::<Vec<_>>(),
            vec![(2, 2), (3, 3), (5, 5)]
        );
        let wrap = &symbols[0];
        assert!(wrap.text.starts_with("a.rs :: wrap\n"));
        assert!(wrap.text.contains("/// Wrap lines to a width."));
        assert!(symbols[2].text.starts_with("a.rs :: Held :: run\n"));
        // A language the parser does not cover keeps the line-window chunker.
        std::fs::write(root.path().join("b.go"), "func x() {}\n".repeat(40)).unwrap();
        let (windows, _, _) = chunks(&workspace, vec!["b.go".into()]).unwrap();
        assert_eq!((windows[0].start, windows[0].end), (1, 32));
        assert_eq!((windows[1].start, windows[1].end), (25, 40));
    }
}
