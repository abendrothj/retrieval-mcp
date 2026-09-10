use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::{path::PathBuf, time::Duration};

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
pub enum Profile {
    A,
    B,
    C,
    #[default]
    D,
}

/// Which implementation answers `search_concept`. This is an operator choice: the model sees one
/// conceptual-search tool and never picks a ranking mechanism.
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Ranker {
    #[default]
    Lexical,
    Semantic,
    Hybrid,
}

impl Ranker {
    pub fn needs_backend(self) -> bool {
        matches!(self, Self::Semantic | Self::Hybrid)
    }
    pub fn needs_index(self) -> bool {
        matches!(self, Self::Lexical | Self::Hybrid)
    }
}

impl Profile {
    pub fn structural(self) -> bool {
        matches!(self, Self::B | Self::D)
    }
    pub fn semantic(self) -> bool {
        matches!(self, Self::C | Self::D)
    }
}

#[derive(Clone, Debug)]
pub struct Config {
    pub root: PathBuf,
    pub profile: Profile,
    pub semantic_command: Option<Vec<String>>,
    pub timeout: Duration,
    pub run_id: Option<String>,
    pub log_file: Option<PathBuf>,
    pub ranker: Ranker,
}

impl Config {
    pub fn parse() -> Result<Option<Self>> {
        let mut args = std::env::args().skip(1);
        let mut config = Self {
            root: PathBuf::new(),
            profile: Profile::D,
            semantic_command: None,
            timeout: Duration::from_secs(30),
            run_id: None,
            log_file: None,
            ranker: Ranker::default(),
        };
        while let Some(flag) = args.next() {
            if flag == "--help" || flag == "-h" {
                println!(
                    "retrieval-mcp --root PATH [--profile A|B|C|D] [--ranker lexical|semantic|hybrid]\n  [--run-id ID] [--semantic-command '[\"program\",\"arg\"]'] [--timeout-seconds 30]\n  [--log-file /absolute/path/events.jsonl]\nJSON invocation logs go to stderr and optionally append to --log-file; stdout is reserved for MCP."
                );
                return Ok(None);
            }
            let value = args
                .next()
                .with_context(|| format!("missing value for {flag}"))?;
            match flag.as_str() {
                "--root" => config.root = value.into(),
                "--log-file" => config.log_file = Some(value.into()),
                "--profile" => {
                    config.profile = serde_json::from_value(serde_json::Value::String(value))
                        .context("profile must be A, B, C, or D")?
                }
                "--ranker" => {
                    config.ranker = serde_json::from_value(serde_json::Value::String(value))
                        .context("ranker must be lexical, semantic, or hybrid")?
                }
                "--semantic-command" => {
                    let command: Vec<String> = serde_json::from_str(&value)
                        .context("semantic command must be a JSON string array")?;
                    ensure!(
                        !command.is_empty() && !command[0].is_empty(),
                        "semantic command cannot be empty"
                    );
                    config.semantic_command = Some(command);
                }
                "--timeout-seconds" => {
                    let seconds: u64 = value.parse().context("timeout must be an integer")?;
                    ensure!(
                        (1..=600).contains(&seconds),
                        "timeout must be 1..600 seconds"
                    );
                    config.timeout = Duration::from_secs(seconds);
                }
                "--run-id" => {
                    ensure!(value.len() <= 256, "run ID too long");
                    config.run_id = Some(value);
                }
                _ => bail!("unknown option: {flag}; use --help"),
            }
        }
        ensure!(
            !config.root.as_os_str().is_empty(),
            "--root PATH is required"
        );
        Ok(Some(config))
    }
}