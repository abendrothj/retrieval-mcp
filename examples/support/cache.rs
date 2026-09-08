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

#[derive(Serialize, Deserialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    identity: Value,
    // Exact content keys avoid hash collisions and reuse a chunk even when its line range moves.
    vectors: BTreeMap<String, Vec<f64>>,
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
        file.take(MAX_CACHE_BYTES + 1).read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() as u64 <= MAX_CACHE_BYTES,
            "semantic cache is too large"
        );
        let snapshot: Self = serde_json::from_slice(&bytes).context(
            "invalid semantic cache; move it aside to rebuild (it has not been overwritten)",
        )?;
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
        let bytes = serde_json::to_vec(self)?;
        ensure!(
            bytes.len() as u64 <= MAX_CACHE_BYTES,
            "semantic cache exceeds size budget"
        );
        let mut temporary =
            tempfile::NamedTempFile::new_in(path.parent().context("cache needs a parent")?)?;
        temporary.write_all(&bytes)?;
        temporary.as_file().sync_all()?;
        temporary
            .persist(path)
            .context("cannot atomically publish semantic cache")?;
        Ok(())
    }
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
    #[cfg(unix)]
    #[test]
    fn cache_rejects_symlink_targets() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("cache.json");
        std::os::unix::fs::symlink("/etc/passwd", &path).unwrap();
        assert!(Snapshot::load(&path).is_err());
    }
}
