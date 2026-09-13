use crate::source::{Workspace, excerpt};
use anyhow::{Context, Result, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{future::Future, pin::Pin, time::Duration};
use tokio::process::Command;

pub type BackendFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T>> + Send + 'a>>;

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExactArgs {
    /// Literal text by default. Set regex=true for a ripgrep regular expression.
    pub query: String,
    /// Optional repository-relative file or directory; defaults to the repository root.
    pub path: Option<String>,
    pub regex: Option<bool>,
    pub case_sensitive: Option<bool>,
    /// Maximum matching lines, 1..100; default 20.
    pub limit: Option<usize>,
    /// Number of matching lines to skip, 0..10000; default 0.
    pub offset: Option<usize>,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct ExactHit {
    pub path: String,
    pub line: usize,
    pub snippet: String,
    pub snippet_truncated: bool,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct Page<T> {
    pub results: Vec<T>,
    pub has_more: bool,
    pub next_offset: Option<usize>,
}

/// A page of exact-search hits plus the one fact that keeps an empty page honest: how many
/// files the query actually scanned. Zero over a nonempty repository means ignore rules or the
/// scope pruned the corpus, not that the text is absent.
#[derive(Debug, Serialize, JsonSchema)]
pub struct ExactPage {
    pub results: Vec<ExactHit>,
    pub has_more: bool,
    pub next_offset: Option<usize>,
    /// Files scanned by this query, from ripgrep's summary; absent when the summary was
    /// unavailable (for example a capped output stream).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub files_searched: Option<usize>,
}

pub fn pagination(limit: Option<usize>, offset: Option<usize>) -> Result<(usize, usize)> {
    let limit = limit.unwrap_or(20);
    let offset = offset.unwrap_or(0);
    ensure!((1..=100).contains(&limit), "limit must be 1..100");
    ensure!(offset <= 10000, "offset must be 0..10000; narrow the query");
    Ok((limit, offset))
}

pub fn validate_query(query: &str) -> Result<()> {
    ensure!(
        !query.trim().is_empty() && query.len() <= 4096,
        "query must contain 1..4096 bytes of nonblank text"
    );
    Ok(())
}

pub trait LexicalBackend: Send + Sync {
    fn search<'a>(
        &'a self,
        workspace: &'a Workspace,
        args: ExactArgs,
    ) -> BackendFuture<'a, ExactPage>;
}

/// Escape ripgrep glob metacharacters so a repository path is matched literally.
fn glob_escape(path: &str) -> String {
    let mut escaped = String::with_capacity(path.len());
    for c in path.chars() {
        if matches!(c, '*' | '?' | '[' | ']' | '{' | '}' | '!' | '\\') {
            escaped.push('\\');
        }
        escaped.push(c);
    }
    escaped
}

pub struct Ripgrep {
    pub timeout: Duration,
    /// Search files that ignore files exclude. Set from `--no-ignore`; `.git`, `target` and
    /// hidden files stay excluded either way.
    pub no_ignore: bool,
}

impl LexicalBackend for Ripgrep {
    fn search<'a>(
        &'a self,
        workspace: &'a Workspace,
        args: ExactArgs,
    ) -> BackendFuture<'a, ExactPage> {
        Box::pin(async move {
            validate_query(&args.query)?;
            let (limit, offset) = pagination(args.limit, args.offset)?;
            // The scope is applied as a glob under the repository root, not as a walk root, so a
            // scoped query sees exactly the corpus an unscoped query and the structural index
            // see: naming a path cannot reach past ignore rules (a nested repository, say) that
            // the walk from the root would prune. One corpus, however the question is phrased.
            let scope = {
                let resolved = workspace.resolve(args.path.as_deref().unwrap_or("."))?;
                let relative = workspace.relative(&resolved)?;
                if relative.is_empty() {
                    None
                } else if resolved.is_dir() {
                    Some(format!("{}/**", glob_escape(&relative)))
                } else {
                    Some(glob_escape(&relative))
                }
            };
            let mut command = Command::new("rg");
            command.current_dir(workspace.root()).args([
                "--no-config",
                "--json",
                "--sort",
                "path",
                "--max-filesize",
                "2M",
                "--glob",
                "!.git/**",
                "--glob",
                "!target/**",
            ]);
            if self.no_ignore {
                command.arg("--no-ignore");
            }
            if let Some(glob) = &scope {
                command.arg("--glob").arg(glob);
            }
            if !args.regex.unwrap_or(false) {
                command.arg("--fixed-strings");
            }
            if !args.case_sensitive.unwrap_or(true) {
                command.arg("--ignore-case");
            }
            command.arg("--").arg(&args.query).arg(".");
            // A bounded capture keeps v1 simple. A streaming parser can replace this for large result sets.
            let (status, output) =
                super::process::run(&mut command, None, self.timeout, 4 * 1024 * 1024).await?;
            ensure!(
                status == 0 || status == 1,
                "ripgrep failed (exit {status}); check regex and path permissions"
            );
            let mut hits = Vec::new();
            let mut seen = 0;
            for line in output
                .split(|b| *b == b'\n')
                .filter(|line| !line.is_empty())
            {
                let event: Value = serde_json::from_slice(line).context("invalid ripgrep JSON")?;
                if event["type"] != "match" {
                    continue;
                }
                if seen < offset {
                    seen += 1;
                    continue;
                }
                let data = &event["data"];
                let path = data["path"]["text"]
                    .as_str()
                    .context("non-UTF-8 paths are unsupported")?
                    .replace('\\', "/");
                let relative = path.strip_prefix("./").unwrap_or(&path).to_owned();
                workspace.resolve(&relative)?;
                let text = data["lines"]["text"]
                    .as_str()
                    .context("non-UTF-8 matches are unsupported")?
                    .trim_end_matches(['\r', '\n']);
                let start = data["submatches"][0]["start"].as_u64().unwrap_or(0) as usize;
                let mut left = start.saturating_sub(120).min(text.len());
                while !text.is_char_boundary(left) {
                    left -= 1;
                }
                let snippet = excerpt(&text[left..], 500);
                let snippet_truncated = left > 0 || snippet.len() < text.len();
                hits.push(ExactHit {
                    path: relative,
                    line: data["line_number"].as_u64().context("missing match line")? as usize,
                    snippet,
                    snippet_truncated,
                });
                if hits.len() > limit {
                    break;
                }
            }
            let has_more = hits.len() > limit;
            hits.truncate(limit);
            // Ripgrep ends the stream with one summary event; its `searches` count is the number
            // of files scanned, which is what makes an empty page interpretable.
            let files_searched = output
                .rsplit(|b| *b == b'\n')
                .find(|line| !line.is_empty())
                .and_then(|line| serde_json::from_slice::<Value>(line).ok())
                .filter(|event| event["type"] == "summary")
                .and_then(|event| event["data"]["stats"]["searches"].as_u64())
                .map(|count| count as usize);
            Ok(ExactPage {
                results: hits,
                has_more,
                next_offset: has_more.then_some(offset + limit),
                files_searched,
            })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn literal_search_ignores_hidden_files_and_paginates() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("a.rs"), "--needle\n--needle\nNEEDLE\n").unwrap();
        std::fs::write(root.path().join(".ignore"), "skip.rs\n").unwrap();
        std::fs::write(root.path().join("skip.rs"), "--needle\n").unwrap();
        std::fs::write(root.path().join(".hidden.rs"), "--needle\n").unwrap();
        let workspace = Workspace::new(root.path()).unwrap();
        let backend = Ripgrep {
            timeout: Duration::from_secs(2),
            no_ignore: false,
        };
        let args = |offset| ExactArgs {
            query: "--needle".into(),
            path: None,
            regex: None,
            case_sensitive: None,
            limit: Some(1),
            offset: Some(offset),
        };
        let first = backend.search(&workspace, args(0)).await.unwrap();
        assert_eq!(first.results[0].line, 1);
        assert_eq!(first.next_offset, Some(1));
        assert_eq!(first.files_searched, Some(1), "only a.rs survives ignore and hidden rules");
        let second = backend.search(&workspace, args(1)).await.unwrap();
        assert_eq!(second.results[0].line, 2);
        assert!(!second.has_more);
        let mut invalid = args(0);
        invalid.path = Some("../outside".into());
        assert!(backend.search(&workspace, invalid).await.is_err());
    }

    #[tokio::test]
    async fn scoped_search_shares_the_corpus_and_no_ignore_reopens_it() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir(root.path().join("sub")).unwrap();
        std::fs::write(root.path().join(".ignore"), "sub/\n").unwrap();
        std::fs::write(root.path().join("sub/hit.rs"), "--needle\n").unwrap();
        let workspace = Workspace::new(root.path()).unwrap();
        let args = || ExactArgs {
            query: "--needle".into(),
            path: Some("sub".into()),
            regex: None,
            case_sensitive: None,
            limit: None,
            offset: None,
        };
        // Naming the ignored directory must not reach past the ignore rules the unscoped walk
        // honors, and the empty page must say the corpus was empty rather than imply absence.
        let honoring = Ripgrep {
            timeout: Duration::from_secs(2),
            no_ignore: false,
        };
        let pruned = honoring.search(&workspace, args()).await.unwrap();
        assert!(pruned.results.is_empty());
        assert_eq!(pruned.files_searched, Some(0));
        let overriding = Ripgrep {
            timeout: Duration::from_secs(2),
            no_ignore: true,
        };
        let found = overriding.search(&workspace, args()).await.unwrap();
        assert_eq!(found.results.len(), 1);
        assert_eq!(found.results[0].path, "sub/hit.rs");
        assert_eq!(found.files_searched, Some(1));
    }
}
