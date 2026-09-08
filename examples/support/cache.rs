use super::{Chunk, cosine};
use anyhow::{Context, Result, ensure};
use retrieval_mcp::source::Workspace;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    fs::{File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

// JSON f64 vectors cost roughly 15 KB per chunk, so this bounds MAX_CHUNKS entries with headroom.
// A binary vector file would cut it about tenfold if the cache ever needs to grow again.
const MAX_CACHE_BYTES: u64 = 512 * 1024 * 1024;

// Vectors live beside the manifest as little-endian f64 so a warm start reads bytes instead of
// parsing millions of JSON numbers. Values are unchanged, so ranking is identical either way.
const VECTOR_FORMAT: &str = "vectors-f64le-v1";
const MAX_MANIFEST_BYTES: u64 = 64 * 1024;

#[derive(PartialEq, Debug)]
pub struct Snapshot {
    identity: Value,
    // Exact content keys avoid hash collisions and reuse a chunk even when its line range moves.
    vectors: BTreeMap<String, Vec<f64>>,
}

#[derive(Serialize, Deserialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
struct Manifest {
    identity: Value,
    format: String,
    entries: usize,
    dimensions: usize,
    vector_bytes: u64,
}

fn vector_path(path: &Path) -> PathBuf {
    path.with_extension("vec")
}

pub async fn open(workspace: &Workspace) -> Result<(PathBuf, File)> {
    let directory = match std::env::var_os("RETRIEVAL_SEMANTIC_CACHE_DIR") {
        Some(path) => {
            let path = PathBuf::from(path);
            ensure!(path.is_absolute(), "cache directory must be absolute");
            path
        }
        None => {
            let path = workspace.root().join(".retrieval-mcp");
            match std::fs::create_dir(&path) {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(e) => return Err(e.into()),
            }
            workspace.resolve(".retrieval-mcp")?
        }
    };
    std::fs::create_dir_all(&directory)?;
    reject_special_file(&directory.join("semantic.lock"))?;
    reject_special_file(&directory.join("semantic.json"))?;
    reject_special_file(&directory.join("semantic.vec"))?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(directory.join("semantic.lock"))?;
    let started = Instant::now();
    // One writer per cache directory; use separate directories to index repositories concurrently.
    loop {
        match lock.try_lock() {
            Ok(()) => break,
            Err(std::fs::TryLockError::WouldBlock) => {
                ensure!(
                    started.elapsed() < Duration::from_secs(30),
                    "semantic cache is busy; retry the request"
                );
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            Err(e) => return Err(anyhow::anyhow!("cannot lock semantic cache: {e}")),
        }
    }
    Ok((directory.join("semantic.json"), lock))
}

fn key(chunk: &Chunk) -> String {
    format!("{}\0{}", chunk.path, chunk.text)
}

fn reject_special_file(path: &Path) -> Result<()> {
    match path.symlink_metadata() {
        Ok(metadata) => ensure!(
            metadata.is_file() && !metadata.file_type().is_symlink(),
            "cache files must be regular files, not symlinks"
        ),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    Ok(())
}

impl Snapshot {
    pub fn load(path: &Path) -> Result<Option<Self>> {
        reject_special_file(path)?;
        let file = match File::open(path) {
            Ok(file) => file,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e.into()),
        };
        let mut bytes = Vec::new();
        file.take(MAX_MANIFEST_BYTES + 1).read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() as u64 <= MAX_MANIFEST_BYTES,
            "semantic cache manifest is too large"
        );
        let manifest: Manifest = serde_json::from_slice(&bytes).context(
            "invalid semantic cache; move it aside to rebuild (it has not been overwritten)",
        )?;
        ensure!(
            manifest.format == VECTOR_FORMAT
                && manifest.entries <= super::MAX_CHUNKS
                && manifest.dimensions <= 8192
                && manifest.vector_bytes <= MAX_CACHE_BYTES,
            "unsupported semantic cache format or size"
        );
        let vectors = vector_path(path);
        reject_special_file(&vectors)?;
        let mut bytes = Vec::new();
        File::open(&vectors)
            .context("semantic cache manifest without its vectors; move both aside to rebuild")?
            .take(MAX_CACHE_BYTES + 1)
            .read_to_end(&mut bytes)?;
        // A torn pair is a mismatch, never a silently shorter snapshot.
        ensure!(
            bytes.len() as u64 == manifest.vector_bytes,
            "semantic cache vectors do not match their manifest; move both aside to rebuild"
        );
        let snapshot = Self {
            identity: manifest.identity,
            vectors: decode(&bytes, manifest.entries, manifest.dimensions)?,
        };
        ensure!(
            snapshot.vectors.len() == manifest.entries,
            "duplicate keys in semantic cache vectors"
        );
        snapshot.validate()?;
        Ok(Some(snapshot))
    }
    pub fn new(identity: Value, chunks: &[Chunk], vectors: &[Vec<f64>]) -> Result<Self> {
        ensure!(chunks.len() == vectors.len(), "incorrect embedding count");
        let snapshot = Self {
            identity,
            vectors: chunks
                .iter()
                .zip(vectors)
                .map(|(c, v)| (key(c), v.clone()))
                .collect(),
        };
        snapshot.validate()?;
        Ok(snapshot)
    }
    fn validate(&self) -> Result<()> {
        ensure!(
            self.identity["version"] == 1 && self.vectors.len() <= super::MAX_CHUNKS,
            "unsupported semantic cache format or size"
        );
        let mut dimensions = None;
        for (key, vector) in &self.vectors {
            ensure!(
                key.len() <= 12500 && vector.len() <= 8192,
                "invalid semantic cache entry size"
            );
            cosine(vector, vector)?;
            ensure!(
                dimensions.is_none_or(|d| d == vector.len()),
                "mixed embedding dimensions in semantic cache"
            );
            dimensions = Some(vector.len());
        }
        Ok(())
    }
    pub fn save(&self, path: &Path) -> Result<()> {
        self.validate()?;
        let parent = path.parent().context("cache needs a parent")?;
        let bytes = encode(&self.vectors)?;
        ensure!(
            bytes.len() as u64 <= MAX_CACHE_BYTES,
            "semantic cache exceeds size budget"
        );
        // Vectors are published first: a manifest never names bytes that are not yet durable.
        let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
        temporary.write_all(&bytes)?;
        temporary.as_file().sync_all()?;
        temporary
            .persist(vector_path(path))
            .context("cannot atomically publish semantic cache vectors")?;
        let manifest = serde_json::to_vec(&Manifest {
            identity: self.identity.clone(),
            format: VECTOR_FORMAT.into(),
            entries: self.vectors.len(),
            dimensions: self.vectors.values().next().map_or(0, Vec::len),
            vector_bytes: bytes.len() as u64,
        })?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
        temporary.write_all(&manifest)?;
        temporary.as_file().sync_all()?;
        temporary
            .persist(path)
            .context("cannot atomically publish semantic cache")?;
        Ok(())
    }
}

fn encode(vectors: &BTreeMap<String, Vec<f64>>) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    // Ordered keys keep the published file byte-identical for identical content.
    for (key, vector) in vectors {
        bytes.extend_from_slice(&u32::try_from(key.len())?.to_le_bytes());
        bytes.extend_from_slice(key.as_bytes());
        for value in vector {
            bytes.extend_from_slice(&value.to_le_bytes());
        }
    }
    Ok(bytes)
}

fn decode(bytes: &[u8], entries: usize, dimensions: usize) -> Result<BTreeMap<String, Vec<f64>>> {
    let mut vectors = BTreeMap::new();
    let mut cursor = 0usize;
    for _ in 0..entries {
        let header = bytes
            .get(cursor..cursor + 4)
            .context("truncated semantic cache entry")?;
        let length = u32::from_le_bytes(header.try_into()?) as usize;
        cursor += 4;
        let key = std::str::from_utf8(
            bytes
                .get(cursor..cursor + length)
                .context("truncated semantic cache key")?,
        )?;
        cursor += length;
        let width = dimensions * 8;
        let values = bytes
            .get(cursor..cursor + width)
            .context("truncated semantic cache vector")?;
        cursor += width;
        vectors.insert(
            key.to_owned(),
            values
                .chunks_exact(8)
                .map(|v| f64::from_le_bytes(v.try_into().expect("eight bytes")))
                .collect(),
        );
    }
    ensure!(cursor == bytes.len(), "trailing bytes in semantic cache");
    Ok(vectors)
}

pub fn reuse(
    previous: Option<&Snapshot>,
    identity: &Value,
    chunks: &[Chunk],
) -> Vec<Option<Vec<f64>>> {
    chunks
        .iter()
        .map(|chunk| {
            previous
                .filter(|s| &s.identity == identity)
                .and_then(|s| s.vectors.get(&key(chunk)))
                .cloned()
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn chunk(text: &str) -> Chunk {
        Chunk {
            path: "a.rs".into(),
            start: 1,
            end: 2,
            text: text.into(),
        }
    }
    #[test]
    fn persists_reuses_invalidates_and_prunes() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("cache.json");
        let identity = json!({"version":1,"root":"/repo","digest":"old"});
        Snapshot::new(
            identity.clone(),
            &[chunk("one"), chunk("two")],
            &[vec![1., 0.], vec![0., 1.]],
        )
        .unwrap()
        .save(&path)
        .unwrap();
        let old = Snapshot::load(&path).unwrap().unwrap();
        let current = [chunk("one"), chunk("changed")];
        let reused = reuse(Some(&old), &identity, &current);
        assert!(reused[0].is_some());
        assert!(reused[1].is_none());
        assert!(
            reuse(
                Some(&old),
                &json!({"version":1,"root":"/repo","digest":"new"}),
                &current
            )
            .iter()
            .all(Option::is_none)
        );
        assert!(
            reuse(
                Some(&old),
                &json!({"version":1,"root":"/other","digest":"old"}),
                &current
            )
            .iter()
            .all(Option::is_none)
        );
        Snapshot::new(identity, &current[..1], &[vec![1., 0.]])
            .unwrap()
            .save(&path)
            .unwrap();
        assert_eq!(Snapshot::load(&path).unwrap().unwrap().vectors.len(), 1);
        let before = std::fs::read(&path).unwrap();
        assert!(Snapshot::new(json!({"version":1}), &[chunk("bad")], &[vec![0., 0.]]).is_err());
        assert_eq!(before, std::fs::read(&path).unwrap());
    }
    #[test]
    fn corrupt_cache_is_reported_without_overwriting() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("cache.json");
        std::fs::write(&path, "incomplete").unwrap();
        assert!(Snapshot::load(&path).is_err());
        assert_eq!(std::fs::read_to_string(path).unwrap(), "incomplete");
    }
    #[test]
    fn vectors_round_trip_beside_the_manifest() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("cache.json");
        let identity = json!({"version":1,"root":"/repo","digest":"old"});
        let original = Snapshot::new(
            identity,
            &[chunk("one"), chunk("two")],
            &[vec![1., 0.5], vec![0.25, 1.]],
        )
        .unwrap();
        original.save(&path).unwrap();
        let vectors = root.path().join("cache.vec");
        // Two entries: a 4-byte key length, the key bytes, then two f64 values each.
        let keys: usize = original.vectors.keys().map(String::len).sum();
        assert_eq!(
            std::fs::metadata(&vectors).unwrap().len() as usize,
            2 * (4 + 2 * 8) + keys
        );
        assert!(std::fs::metadata(&path).unwrap().len() < 512);
        assert_eq!(Snapshot::load(&path).unwrap().unwrap(), original);
    }

    #[test]
    fn truncated_vectors_are_reported_without_overwriting() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("cache.json");
        Snapshot::new(
            json!({"version":1,"root":"/repo"}),
            &[chunk("one")],
            &[vec![1., 0.]],
        )
        .unwrap()
        .save(&path)
        .unwrap();
        let vectors = root.path().join("cache.vec");
        let before = std::fs::read(&vectors).unwrap();
        std::fs::write(&vectors, &before[..before.len() - 1]).unwrap();
        assert!(Snapshot::load(&path).is_err());
        assert_eq!(
            std::fs::read(&vectors).unwrap().len(),
            before.len() - 1,
            "a rejected cache is preserved for inspection"
        );
        std::fs::remove_file(&vectors).unwrap();
        assert!(Snapshot::load(&path).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn cache_rejects_symlink_targets() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("cache.json");
        std::os::unix::fs::symlink("/etc/passwd", &path).unwrap();
        assert!(Snapshot::load(&path).is_err());
    }
}
