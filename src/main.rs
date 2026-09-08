use anyhow::Result;
use retrieval_mcp::{config::Config, tools::RetrievalServer};
use rmcp::ServiceExt;

#[tokio::main]
async fn main() -> Result<()> {
    let Some(config) = Config::parse()? else {
        return Ok(());
    };
    tracing_subscriber::fmt()
        .json()
        .with_writer(std::io::stderr)
        .with_target(false)
        .with_max_level(tracing::Level::INFO)
        .init();
    let server = RetrievalServer::new(config)?;
    tracing::info!(event = "server_start", profile = ?server.config.profile);
    server
        .serve(rmcp::transport::stdio())
        .await?
        .waiting()
        .await?;
    Ok(())
}
