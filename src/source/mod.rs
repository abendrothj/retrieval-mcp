use anyhow::{Context, Result, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::{
    fs::File,
    io::Read,
    path::{Component, Path, PathBuf},
};

pub const MAX_FILE_BYTES: u64 = 2 * 1024 * 1024;
pub const MAX_RESPONSE_BYTES: usize = 64 * 1024;

#[derive(Clone, Debug)]
pub struct Workspace {
    root: PathBuf,
}

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ReadArgs {
    /// Repository-relative file path. Absolute paths and symlinks are rejected.
    pub path: String,
    /// First line, inclusive and 1-based. Defaults to 1.
    pub start_line: Option<usize>,
    /// Last line, inclusive. Defaults to 100 lines from start. At most 500 lines per call.
    pub end_line: Option<usize>,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct SourceLine {
    pub line: usize,
    pub text: String,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct SourceResult {
    pub path: String,
    pub lines: Vec<SourceLine>,
    pub total_lines: usize,
    pub truncated: bool,
    pub next_line: Option<usize>,
}

impl Workspace {
    pub fn new(root: &Path) -> Result<Self> {
        let root = root
            .canonicalize()
            .context("repository root does not exist")?;
        ensure!(root.is_dir(), "repository root must be a directory");
        Ok(Self { root })
    }
    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn resolve(&self, path: &str) -> Result<PathBuf> {
        ensure!(path.len() <= 4096, "path is too long");
        let relative = Path::new(path);
        ensure!(!relative.is_absolute(), "use a repository-relative path");
        let mut candidate = self.root.clone();
        for component in relative.components() {
            match component {
                Component::Normal(part) => {
                    candidate.push(part);
                    let metadata = candidate
                        .symlink_metadata()
                        .context("path is missing or inaccessible")?;
                    ensure!(
                        !metadata.file_type().is_symlink(),
                        "symlinks are not allowed"
                    );
                }
                Component::CurDir => {}
                _ => anyhow::bail!("path must stay inside the repository"),
            }
        }
        let canonical = candidate
            .canonicalize()
            .context("cannot resolve repository path")?;
        ensure!(
            canonical.starts_with(&self.root),
            "path escapes the repository"
        );
        Ok(canonical)
    }

    pub fn relative(&self, path: &Path) -> Result<String> {
        Ok(path
            .strip_prefix(&self.root)
            .context("path escapes the repository")?
            .to_str()
            .context("non-UTF-8 paths are unsupported")?
            .replace('\\', "/"))
    }

    pub fn text(&self, path: &str) -> Result<String> {
        let path = self.resolve(path)?;
        let metadata = path.metadata().context("cannot inspect file")?;
        ensure!(metadata.is_file(), "path must be a regular file");
        ensure!(
            metadata.len() <= MAX_FILE_BYTES,
            "file exceeds the 2 MiB source limit"
        );
        let file = File::open(path).context("cannot open source file")?;
        let mut bytes = Vec::new();
        file.take(MAX_FILE_BYTES + 1)
            .read_to_end(&mut bytes)
            .context("cannot read source file")?;
        ensure!(
            bytes.len() as u64 <= MAX_FILE_BYTES,
            "file exceeds the 2 MiB source limit"
        );
        ensure!(!bytes.contains(&0), "binary files are unsupported");
        String::from_utf8(bytes).context("source file must be UTF-8")
    }

    pub fn read(&self, args: ReadArgs) -> Result<SourceResult> {
        let start = args.start_line.unwrap_or(1);
        ensure!(start > 0, "start_line must be at least 1");
        let end = args.end_line.unwrap_or(start.saturating_add(99));
        ensure!(
            end >= start && end - start < 500,
            "request an inclusive range of 1..500 lines"
        );
        let text = self.text(&args.path)?;
        let all: Vec<_> = text.lines().collect();
        ensure!(start <= all.len().max(1), "start_line is past end of file");
        let mut bytes = 0;
        let mut lines = Vec::new();
        for (i, line) in all.iter().enumerate().take(end).skip(start - 1) {
            // Budget escaped JSON, including line records, rather than only raw text.
            let size = serde_json::to_string(line)?.len() + 64;
            ensure!(
                size <= MAX_RESPONSE_BYTES / 2,
                "source line is too large; use search_exact for a bounded excerpt"
            );
            if bytes + size > MAX_RESPONSE_BYTES / 2 {
                break;
            }
            bytes += size;
            lines.push(SourceLine {
                line: i + 1,
                text: (*line).into(),
            });
        }
        let last = lines.last().map_or(0, |line| line.line);
        let next_line = (last < all.len()).then_some(last + 1);
        Ok(SourceResult {
            path: self.relative(&self.resolve(&args.path)?)?,
            lines,
            total_lines: all.len(),
            truncated: next_line.is_some(),
            next_line,
        })
    }
}

pub fn excerpt(text: &str, max_chars: usize) -> String {
    text.chars().take(max_chars).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bounded_reads_and_invalid_ranges() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("code.rs"), "one\ntwo\nthree\n").unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        let result = ws
            .read(ReadArgs {
                path: "code.rs".into(),
                start_line: Some(2),
                end_line: Some(2),
            })
            .unwrap();
        assert_eq!(result.lines[0].text, "two");
        assert_eq!(result.next_line, Some(3));
        assert!(ws.resolve("../secret").is_err());
        assert!(ws.resolve("/etc/passwd").is_err());
        assert!(
            ws.read(ReadArgs {
                path: "code.rs".into(),
                start_line: Some(0),
                end_line: None
            })
            .is_err()
        );
        assert!(
            ws.read(ReadArgs {
                path: "code.rs".into(),
                start_line: None,
                end_line: Some(501)
            })
            .is_err()
        );
    }
    #[cfg(unix)]
    #[test]
    fn rejects_symlinks_and_binary_files() {
        let dir = tempfile::tempdir().unwrap();
        std::os::unix::fs::symlink("/etc", dir.path().join("escape")).unwrap();
        std::fs::write(dir.path().join("binary"), [0, 1, 2]).unwrap();
        let ws = Workspace::new(dir.path()).unwrap();
        assert!(ws.text("escape/passwd").is_err());
        assert!(ws.text("binary").is_err());
        assert!(ws.text(".").is_err());
    }
}
