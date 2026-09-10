use super::lexical::{BackendFuture, pagination, validate_query};
use crate::index::SymbolLocation;
use crate::source::{Workspace, excerpt};
use anyhow::{Context, Result, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, time::Duration};
use tokio::process::Command;

#[derive(Debug, Deserialize, JsonSchema)]
pub struct ConceptArgs {
    /// Natural-language description of the behavior or concept to locate.
    pub query: String,
    /// Optional repository-relative file or directory to restrict results.
    pub path: Option<String>,
    /// Maximum results, 1..100; default 10.
    pub limit: Option<usize>,
    /// Result offset, 0..10000; default 0. Ranking must be stable to paginate.
    pub offset: Option<usize>,
    /// Optional extras per hit. Only "excerpt" is supported; omit it for identity-sized rows.
    pub fields: Option<Vec<String>>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SemanticRequest {
    pub protocol_version: u32,
    pub root: String,
    pub query: String,
    pub limit: usize,
    pub offset: usize,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BackendHit {
    pub path: String,
    pub start_line: usize,
    pub end_line: usize,
    pub score: f64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SemanticResponse {
    pub protocol_version: u32,
    pub backend: String,
    pub index_note: String,
    pub results: Vec<BackendHit>,
    pub has_more: bool,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct ConceptHit {
    pub path: String,
    pub start_line: usize,
    pub end_line: usize,
    pub score: f64,
    /// The enclosing indexed definition and its call-graph degrees, when the index covers the file.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub symbol: Option<SymbolLocation>,
    /// Present only when the caller asked for the "excerpt" field.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub excerpt: Option<String>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub excerpt_truncated: bool,
}

#[derive(Debug, Serialize, JsonSchema)]
pub struct ConceptResult {
    pub results: Vec<ConceptHit>,
    pub has_more: bool,
    pub next_offset: Option<usize>,
    pub backend: String,
    pub index_note: String,
    pub source_verification: String,
}

pub trait SemanticBackend: Send + Sync {
    fn search<'a>(
        &'a self,
        workspace: &'a Workspace,
        args: ConceptArgs,
    ) -> BackendFuture<'a, ConceptResult>;
}

pub struct CommandSemantic {
    pub command: Vec<String>,
    pub timeout: Duration,
}
impl SemanticBackend for CommandSemantic {
    fn search<'a>(
        &'a self,
        workspace: &'a Workspace,
        args: ConceptArgs,
    ) -> BackendFuture<'a, ConceptResult> {
        Box::pin(async move {
            validate_query(&args.query)?;
            let (limit, offset) = pagination(Some(args.limit.unwrap_or(10)), args.offset)?;
            let include_excerpt = args
                .fields
                .as_deref()
                .is_some_and(|fields| fields.iter().any(|field| field == "excerpt"));
            let program = self
                .command
                .first()
                .context("semantic command cannot be empty")?;
            let mut command = Command::new(program);
            command
                .args(&self.command[1..])
                .current_dir(workspace.root());
            let request = SemanticRequest {
                protocol_version: 1,
                root: workspace
                    .root()
                    .to_str()
                    .context("root must be UTF-8")?
                    .into(),
                query: args.query,
                limit,
                offset,
            };
            let (status, output) = super::process::run(
                &mut command,
                Some(serde_json::to_vec(&request)?),
                self.timeout,
                1024 * 1024,
            )
            .await?;
            ensure!(
                status == 0,
                "semantic backend failed (exit {status}); run the adapter directly to diagnose its configuration"
            );
            let response: SemanticResponse = serde_json::from_slice(&output)
                .context("semantic backend must return the documented version-1 JSON response")?;
            let workspace = workspace.clone();
            tokio::task::spawn_blocking(move || {
                validate_response(&workspace, response, limit, offset, include_excerpt)
            })
            .await?
        })
    }
}

fn validate_response(
    workspace: &Workspace,
    response: SemanticResponse,
    limit: usize,
    offset: usize,
    include_excerpt: bool,
) -> Result<ConceptResult> {
    ensure!(
        response.protocol_version == 1,
        "unsupported semantic protocol_version"
    );
    ensure!(
        response.results.len() <= limit,
        "semantic backend exceeded requested limit"
    );
    ensure!(
        !response.has_more || !response.results.is_empty(),
        "semantic backend returned an empty page with has_more=true"
    );
    ensure!(
        response.backend.len() <= 200 && response.index_note.len() <= 1000,
        "semantic backend metadata is too large"
    );
    let mut results = Vec::new();
    let mut files = BTreeMap::new();
    for hit in response.results {
        ensure!(hit.score.is_finite(), "semantic score must be finite");
        ensure!(
            hit.start_line > 0
                && hit.end_line >= hit.start_line
                && hit.end_line - hit.start_line < 500,
            "semantic backend returned an invalid line range (expected 1..500 lines)"
        );
        let path = workspace.relative(&workspace.resolve(&hit.path)?)?;
        if !files.contains_key(&path) {
            // Cap total validation I/O for a single page while retaining current source excerpts.
            ensure!(
                files.len() < 100,
                "semantic backend returned too many files"
            );
            files.insert(path.clone(), workspace.text(&path)?);
        }
        let source = &files[&path];
        ensure!(
            hit.end_line <= source.lines().count(),
            "semantic backend line range is stale or outside the source; rebuild its index"
        );
        let text = source
            .lines()
            .skip(hit.start_line - 1)
            .take(hit.end_line - hit.start_line + 1)
            .collect::<Vec<_>>()
            .join("\n");
        let shortened = excerpt(&text, 500);
        let excerpt_truncated = include_excerpt && shortened.len() < text.len();
        results.push(ConceptHit {
            path,
            start_line: hit.start_line,
            end_line: hit.end_line,
            score: hit.score,
            symbol: None,
            excerpt: include_excerpt.then_some(shortened),
            excerpt_truncated,
        });
    }
    let next_offset = response.has_more.then_some(offset + results.len());
    Ok(ConceptResult { results, has_more: response.has_more, next_offset, backend: response.backend, index_note: response.index_note,
        source_verification: "Rows name the enclosing indexed definition and are re-verified against current source; excerpts are returned only when requested. Backend ranking/index freshness is not verified; scores are backend-specific, not confidence probabilities.".into() })
}

/// Build verified rows from any ranker's regions: the server always re-reads current source.
pub fn rows(
    workspace: &Workspace,
    regions: Vec<crate::index::RankedRegion>,
    limit: usize,
    offset: usize,
    include_excerpt: bool,
    backend: &str,
    index_note: &str,
) -> Result<ConceptResult> {
    let has_more = regions.len() > offset + limit;
    let response = SemanticResponse {
        protocol_version: 1,
        backend: backend.into(),
        index_note: index_note.into(),
        has_more,
        results: regions
            .into_iter()
            .skip(offset)
            .take(limit)
            .map(|region| BackendHit {
                path: region.path,
                start_line: region.start_line,
                end_line: region.end_line.min(region.start_line + 499),
                score: region.score,
            })
            .collect(),
    };
    validate_response(workspace, response, limit, offset, include_excerpt)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_backend_path_escapes_stale_lines_and_limits() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("safe.rs"), "fn safe() {}\n").unwrap();
        let ws = Workspace::new(root.path()).unwrap();
        let response = |path: &str, end_line| SemanticResponse {
            protocol_version: 1,
            backend: "test".into(),
            index_note: "test".into(),
            has_more: false,
            results: vec![BackendHit {
                path: path.into(),
                start_line: 1,
                end_line,
                score: 0.8,
            }],
        };
        assert!(validate_response(&ws, response("../outside", 1), 20, 0, false).is_err());
        assert!(validate_response(&ws, response("safe.rs", 2), 20, 0, false).is_err());
        assert!(validate_response(&ws, response("safe.rs", 1), 0, 0, false).is_err());
        // Identity-sized rows by default: no source text until the caller asks for it.
        let lean = validate_response(&ws, response("safe.rs", 1), 20, 0, false).unwrap();
        assert_eq!(lean.results[0].excerpt, None);
        assert!(!lean.results[0].excerpt_truncated);
        let full = validate_response(&ws, response("safe.rs", 1), 20, 0, true).unwrap();
        assert_eq!(full.results[0].excerpt.as_deref(), Some("fn safe() {}"));
    }
}
