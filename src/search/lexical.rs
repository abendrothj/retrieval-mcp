use crate::source::{Workspace, excerpt};
use anyhow::{Context, Result, ensure};
use grep_matcher::Matcher;
use grep_regex::RegexMatcherBuilder;
use grep_searcher::{BinaryDetection, SearcherBuilder, Sink, SinkMatch};
use ignore::WalkBuilder;
use ignore::overrides::OverrideBuilder;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::{
    future::Future,
    pin::Pin,
    time::{Duration, Instant},
};

pub type BackendFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T>> + Send + 'a>>;

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExactArgs {
    /// Literal text by default. Set regex=true for a ripgrep regular expression.
    pub query: Option<String>,
    /// Several patterns answered in one call, each hit tagged with the pattern that found it.
    /// Use this instead of consecutive `search_exact` calls: one call costs one round trip, and
    /// a round trip re-sends the whole conversation. Exactly one of `query` or `queries`.
    pub queries: Option<Vec<String>>,
    /// Optional repository-relative file or directory; defaults to the repository root.
    pub path: Option<String>,
    pub regex: Option<bool>,
    pub case_sensitive: Option<bool>,
    /// Maximum matching lines per pattern, 1..100; default 20.
    pub limit: Option<usize>,
    /// Number of matching lines to skip, 0..10000; default 0.
    pub offset: Option<usize>,
}

impl ExactArgs {
    /// The patterns this call asks for, in order, or an error if the call names none or both.
    pub fn patterns(&self) -> Result<Vec<String>> {
        match (&self.query, &self.queries) {
            (Some(_), Some(_)) => anyhow::bail!("pass query or queries, not both"),
            (None, None) => anyhow::bail!("pass query, or queries for several patterns at once"),
            (Some(one), None) => Ok(vec![one.clone()]),
            (None, Some(many)) => {
                ensure!(
                    (1..=8).contains(&many.len()),
                    "queries must name 1..8 patterns; more than that is a scan, not a search"
                );
                Ok(many.clone())
            }
        }
    }
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct ExactHit {
    /// Which pattern found this line. Present only when the call asked for several, so a
    /// single-pattern response is byte-identical to what it has always been.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub query: Option<String>,
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
    /// Files this query actually scanned. Zero over a nonempty repository means ignore rules or
    /// the scope pruned the corpus, not that the text is absent.
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

/// Escape glob metacharacters so a repository path is matched literally.
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

/// Exact search over ripgrep's own engine, linked in rather than spawned: `grep-searcher` and
/// `grep-regex` for matching, `ignore` for the walk. Same regex dialect, same ignore rules, and
/// no `rg` binary on PATH for a user to install or a sandbox to withhold.
pub struct Ripgrep {
    pub timeout: Duration,
    /// Search files that ignore files exclude. Set from `--no-ignore`; `.git`, `target` and
    /// hidden files stay excluded either way.
    pub no_ignore: bool,
}

/// Collects matching lines, skipping `offset` of them and stopping one past `limit` so the page
/// knows whether more exist. Search continues over every eligible file either way, because
/// `files_searched` is only worth reporting if it counts what was actually scanned.
struct Collector<'a> {
    matcher: &'a grep_regex::RegexMatcher,
    /// The pattern this collector is running, carried onto every hit when the call asked for
    /// more than one.
    query: Option<&'a str>,
    path: &'a str,
    hits: &'a mut Vec<ExactHit>,
    seen: &'a mut usize,
    offset: usize,
    wanted: usize,
    deadline: Instant,
}

impl Sink for Collector<'_> {
    type Error = std::io::Error;

    fn matched(
        &mut self,
        _searcher: &grep_searcher::Searcher,
        matched: &SinkMatch<'_>,
    ) -> Result<bool, std::io::Error> {
        if Instant::now() >= self.deadline {
            return Err(std::io::Error::other("timed out"));
        }
        if *self.seen < self.offset {
            *self.seen += 1;
            return Ok(true);
        }
        if self.hits.len() >= self.wanted {
            // Enough for this page; keep walking so the file count stays honest.
            return Ok(false);
        }
        let Ok(text) = std::str::from_utf8(matched.bytes()) else {
            return Ok(true);
        };
        let text = text.trim_end_matches(['\r', '\n']);
        let start = self
            .matcher
            .find(text.as_bytes())
            .ok()
            .flatten()
            .map_or(0, |found| found.start());
        let mut left = start.saturating_sub(120).min(text.len());
        while !text.is_char_boundary(left) {
            left -= 1;
        }
        let snippet = excerpt(&text[left..], 500);
        let snippet_truncated = left > 0 || snippet.len() < text.len();
        self.hits.push(ExactHit {
            query: self.query.map(str::to_owned),
            path: self.path.to_owned(),
            line: matched.line_number().unwrap_or(0) as usize,
            snippet,
            snippet_truncated,
        });
        Ok(true)
    }
}

impl LexicalBackend for Ripgrep {
    fn search<'a>(
        &'a self,
        workspace: &'a Workspace,
        args: ExactArgs,
    ) -> BackendFuture<'a, ExactPage> {
        Box::pin(async move {
            let patterns = args.patterns()?;
            for pattern in &patterns {
                validate_query(pattern)?;
            }
            let batched = patterns.len() > 1;
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
            // One matcher per pattern, one walk for all of them: batching exists to remove a
            // round trip, and re-walking the tree per pattern would trade the model's tokens for
            // the server's seconds.
            let matchers = patterns
                .iter()
                .map(|pattern| {
                    RegexMatcherBuilder::new()
                        .fixed_strings(!args.regex.unwrap_or(false))
                        .case_insensitive(!args.case_sensitive.unwrap_or(true))
                        .line_terminator(Some(b'\n'))
                        .build(pattern)
                        .context("invalid regular expression")
                })
                .collect::<Result<Vec<_>>>()?;
            let workspace = workspace.clone();
            let (timeout, no_ignore) = (self.timeout, self.no_ignore);
            // Walking and searching are blocking filesystem work; keep them off the reactor.
            tokio::task::spawn_blocking(move || {
                let deadline = Instant::now() + timeout;
                let mut overrides = OverrideBuilder::new(workspace.root());
                overrides.add("!.git/**")?;
                overrides.add("!target/**")?;
                if let Some(glob) = &scope {
                    overrides.add(glob)?;
                }
                let mut walk = WalkBuilder::new(workspace.root());
                walk.overrides(overrides.build()?)
                    .hidden(true)
                    .max_filesize(Some(2 * 1024 * 1024))
                    .sort_by_file_path(std::path::Path::cmp);
                if no_ignore {
                    walk.ignore(false)
                        .git_ignore(false)
                        .git_global(false)
                        .git_exclude(false)
                        .parents(false);
                }
                let mut searcher = SearcherBuilder::new()
                    .binary_detection(BinaryDetection::quit(b'\x00'))
                    .line_number(true)
                    .build();
                // Per-pattern pages, so a batched call returns exactly what the same patterns
                // would have returned one call at a time.
                let mut pages: Vec<(Vec<ExactHit>, usize)> =
                    patterns.iter().map(|_| (Vec::new(), 0usize)).collect();
                let mut files_searched = 0usize;
                for entry in walk.build() {
                    ensure!(
                        Instant::now() < deadline,
                        "search timed out; narrow the query or increase --timeout-seconds"
                    );
                    let entry = entry.context("cannot walk the repository")?;
                    if !entry.file_type().is_some_and(|kind| kind.is_file()) {
                        continue;
                    }
                    let Ok(relative) = workspace.relative(entry.path()) else {
                        continue;
                    };
                    files_searched += 1;
                    for (index, matcher) in matchers.iter().enumerate() {
                        let (hits, seen) = &mut pages[index];
                        let collector = Collector {
                            matcher,
                            query: batched.then(|| patterns[index].as_str()),
                            path: &relative,
                            hits,
                            seen,
                            offset,
                            wanted: limit + 1,
                            deadline,
                        };
                        match searcher.search_path(matcher, entry.path(), collector) {
                            Ok(()) => {}
                            // An unreadable or undecodable file is skipped, exactly as ripgrep
                            // skips it; only a timeout stops the search.
                            Err(error) if error.to_string().contains("timed out") => {
                                anyhow::bail!(
                                    "search timed out; narrow the query or increase \
                                     --timeout-seconds"
                                )
                            }
                            Err(_) => continue,
                        }
                    }
                }
                let mut results = Vec::new();
                let mut has_more = false;
                for (mut hits, _) in pages {
                    has_more |= hits.len() > limit;
                    hits.truncate(limit);
                    results.append(&mut hits);
                }
                Ok(ExactPage {
                    results,
                    has_more,
                    next_offset: has_more.then_some(offset + limit),
                    files_searched: Some(files_searched),
                })
            })
            .await?
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
            query: Some("--needle".into()),
            queries: None,
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

    /// A batched call must return exactly what the same patterns return one call at a time.
    ///
    /// That equality is the whole claim: batching exists to remove a round trip, and a round trip
    /// is only worth removing if nothing about the answer changes. Hits carry the pattern that
    /// found them, and a single-pattern call carries no tag at all, so an existing response is
    /// byte-identical.
    #[tokio::test]
    async fn a_batched_search_equals_the_same_searches_run_one_at_a_time() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("a.rs"), "alpha here\nbeta here\nalpha again\n").unwrap();
        std::fs::write(root.path().join("b.rs"), "gamma here\nbeta twice\n").unwrap();
        let workspace = Workspace::new(root.path()).unwrap();
        let backend = Ripgrep {
            timeout: Duration::from_secs(2),
            no_ignore: false,
        };
        let single = |pattern: &str| ExactArgs {
            query: Some(pattern.into()),
            queries: None,
            path: None,
            regex: None,
            case_sensitive: None,
            limit: None,
            offset: None,
        };
        let mut sequential = Vec::new();
        for pattern in ["alpha", "beta", "gamma"] {
            let page = backend.search(&workspace, single(pattern)).await.unwrap();
            assert!(page.results.iter().all(|hit| hit.query.is_none()), "no tag when unbatched");
            for hit in page.results {
                sequential.push((pattern.to_string(), hit.path, hit.line, hit.snippet));
            }
        }
        let batched = backend
            .search(
                &workspace,
                ExactArgs {
                    query: None,
                    queries: Some(
                        ["alpha", "beta", "gamma"].iter().map(|s| s.to_string()).collect(),
                    ),
                    path: None,
                    regex: None,
                    case_sensitive: None,
                    limit: None,
                    offset: None,
                },
            )
            .await
            .unwrap();
        let merged: Vec<_> = batched
            .results
            .into_iter()
            .map(|hit| (hit.query.unwrap(), hit.path, hit.line, hit.snippet))
            .collect();
        assert_eq!(merged, sequential);
        // One walk, not three: the file count is what a single search would report.
        assert_eq!(batched.files_searched, Some(2));
    }

    #[tokio::test]
    async fn a_call_must_name_patterns_exactly_once() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("a.rs"), "alpha\n").unwrap();
        let workspace = Workspace::new(root.path()).unwrap();
        let backend = Ripgrep {
            timeout: Duration::from_secs(2),
            no_ignore: false,
        };
        let build = |query: Option<&str>, queries: Option<Vec<&str>>| ExactArgs {
            query: query.map(str::to_owned),
            queries: queries.map(|all| all.into_iter().map(str::to_owned).collect()),
            path: None,
            regex: None,
            case_sensitive: None,
            limit: None,
            offset: None,
        };
        assert!(backend.search(&workspace, build(None, None)).await.is_err());
        assert!(backend
            .search(&workspace, build(Some("alpha"), Some(vec!["beta"])))
            .await
            .is_err());
        assert!(backend.search(&workspace, build(None, Some(vec![]))).await.is_err());
        let too_many: Vec<&str> = vec!["a", "b", "c", "d", "e", "f", "g", "h", "i"];
        assert!(backend.search(&workspace, build(None, Some(too_many))).await.is_err());
    }

    #[tokio::test]
    async fn scoped_search_shares_the_corpus_and_no_ignore_reopens_it() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir(root.path().join("sub")).unwrap();
        std::fs::write(root.path().join(".ignore"), "sub/\n").unwrap();
        std::fs::write(root.path().join("sub/hit.rs"), "--needle\n").unwrap();
        let workspace = Workspace::new(root.path()).unwrap();
        let args = || ExactArgs {
            query: Some("--needle".into()),
            queries: None,
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
