use anyhow::{Context, Result, ensure};
use std::{process::Stdio, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt},
    process::Command,
};

async fn bounded_read(reader: impl AsyncRead + Unpin, cap: usize) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    reader.take(cap as u64 + 1).read_to_end(&mut bytes).await?;
    ensure!(
        bytes.len() <= cap,
        "backend output limit exceeded; narrow the query"
    );
    Ok(bytes)
}

pub async fn run(
    command: &mut Command,
    input: Option<Vec<u8>>,
    timeout: Duration,
    cap: usize,
) -> Result<(i32, Vec<u8>)> {
    let mut child = command
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .context("cannot start backend; check executable and PATH")?;
    let stdout = child.stdout.take().context("missing backend stdout")?;
    let stderr = child.stderr.take().context("missing backend stderr")?;
    let stdin = child.stdin.take();
    let task = async {
        let write = async {
            if let (Some(mut stdin), Some(input)) = (stdin, input) {
                stdin.write_all(&input).await?;
            }
            Ok::<_, anyhow::Error>(())
        };
        let (out, _err, _, status) = tokio::try_join!(
            bounded_read(stdout, cap),
            bounded_read(stderr, 64 * 1024),
            write,
            async { Ok::<_, anyhow::Error>(child.wait().await?) }
        )?;
        Ok((status.code().unwrap_or(-1), out))
    };
    match tokio::time::timeout(timeout, task).await {
        Ok(Ok(result)) => Ok(result),
        result => {
            let _ = child.kill().await;
            match result {
                Ok(Err(error)) => Err(error),
                _ => anyhow::bail!(
                    "backend timed out; narrow the query or increase --timeout-seconds"
                ),
            }
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[tokio::test]
    async fn bounds_output_and_terminates_timeout() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "printf 123456789"]);
        let error = run(&mut command, None, Duration::from_secs(2), 4)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("output limit"));
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "exec sleep 5"]);
        let started = std::time::Instant::now();
        let error = run(&mut command, None, Duration::from_millis(50), 100)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("timed out"));
        assert!(started.elapsed() < Duration::from_secs(2));
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "exit 7"]);
        assert_eq!(
            run(&mut command, None, Duration::from_secs(2), 100)
                .await
                .unwrap()
                .0,
            7
        );
    }
}
