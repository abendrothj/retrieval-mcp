use anyhow::Result;
use retrieval_mcp::{config::Config, tools::RetrievalServer};
use rmcp::ServiceExt;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::task::{Context, Poll};
use std::time::Duration;
use tokio::io::{AsyncRead, ReadBuf, Stdin};
use tokio::time::{Sleep, sleep};

/// How long to let a just-read request reach its handler before end-of-input is believed, and how
/// often to re-check afterwards. Both are deliberate simplifications over a notifier: the wait
/// happens once, at shutdown, after stdin has already ended.
const DISPATCH_GRACE: Duration = Duration::from_millis(50);
const DRAIN_POLL: Duration = Duration::from_millis(10);

/// Standard input that refuses to report end-of-input while the server still owes a reply.
///
/// A client that writes its requests and closes stdin - a pipeline, a script, a test - otherwise
/// races the server: rmcp treats end of input as a shutdown signal, so a slow first structural call
/// could finish internally while its response was discarded. A request the server accepted is a
/// reply it owes. Reads pass straight through, so this cannot run ahead of the consumer the way a
/// buffered copy does; only the final `Ok(0)` is held back.
struct DrainingStdin {
    inner: Stdin,
    inflight: Arc<AtomicUsize>,
    waiting: Option<Pin<Box<Sleep>>>,
    graced: bool,
}

impl AsyncRead for DrainingStdin {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let this = self.get_mut();
        if let Some(timer) = this.waiting.as_mut() {
            match timer.as_mut().poll(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(()) => this.waiting = None,
            }
        }
        if this.graced && this.inflight.load(Ordering::SeqCst) == 0 {
            return Poll::Ready(Ok(()));
        }
        if this.graced {
            this.waiting = Some(Box::pin(sleep(DRAIN_POLL)));
            return self_poll_again(this, cx);
        }
        let before = buf.filled().len();
        match Pin::new(&mut this.inner).poll_read(cx, buf) {
            Poll::Ready(Ok(())) if buf.filled().len() == before => {
                // Real end of input. Give whatever was just read time to reach a handler, then
                // hold the shutdown open until the count of owed replies falls to zero.
                this.graced = true;
                this.waiting = Some(Box::pin(sleep(DISPATCH_GRACE)));
                self_poll_again(this, cx)
            }
            other => other,
        }
    }
}

/// Re-arm the task after installing a timer, so the wait resumes without a lost wakeup.
fn self_poll_again(this: &mut DrainingStdin, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
    if let Some(timer) = this.waiting.as_mut() {
        let _ = timer.as_mut().poll(cx);
    }
    Poll::Pending
}

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
    tracing::info!(event = "server_start", tools = ?server.config.label);
    let input = DrainingStdin {
        inner: tokio::io::stdin(),
        inflight: Arc::clone(&server.inflight),
        waiting: None,
        graced: false,
    };
    server
        .serve((input, tokio::io::stdout()))
        .await?
        .waiting()
        .await?;
    Ok(())
}
