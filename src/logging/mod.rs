use anyhow::{Context, Result};
use serde_json::{Value, json};
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::Path,
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Instant, SystemTime, UNIX_EPOCH},
};

pub struct InvocationLog {
    pub session_id: String,
    pub run_id: Option<String>,
    pub profile: String,
    sequence: AtomicU64,
    file: Option<Mutex<File>>,
}

impl InvocationLog {
    pub fn new(profile: String, run_id: Option<String>, path: Option<&Path>) -> Result<Self> {
        let file = path
            .map(|path| {
                let mut options = OpenOptions::new();
                options.create(true).append(true);
                #[cfg(unix)]
                {
                    use std::os::unix::fs::OpenOptionsExt;
                    options.mode(0o600);
                }
                options
                    .open(path)
                    .map(Mutex::new)
                    .context("cannot open --log-file; parent directory must exist")
            })
            .transpose()?;
        Ok(Self {
            session_id: format!("{}-{}", std::process::id(), now_ms()),
            run_id,
            profile,
            sequence: AtomicU64::new(0),
            file,
        })
    }
    pub fn start(&self, tool: &str, args: &Value, request_id: &Value) -> CallLog<'_> {
        let sequence = self.sequence.fetch_add(1, Ordering::Relaxed) + 1;
        let mut fields = json!({ "schema_version": 1, "session_id": self.session_id, "run_id": self.run_id,
            "profile": self.profile, "sequence": sequence, "request_id": request_id, "tool": tool, "arguments": args });
        fields["event"] = json!("tool_start");
        fields["timestamp_ms"] = json!(now_ms());
        self.write_record(&fields);
        CallLog {
            fields,
            start: Instant::now(),
            finished: false,
            owner: self,
        }
    }
    // This is the persistence boundary for invocation events. Keep its versioned JSON schema stable.
    fn write_record(&self, record: &Value) {
        let line = format!("{record}\n");
        if std::io::stderr().lock().write_all(line.as_bytes()).is_err() {
            tracing::error!("failed to write invocation log to stderr");
        }
        if let Some(file) = &self.file {
            match file.lock() {
                Ok(mut file) => {
                    if file.write_all(line.as_bytes()).is_err() {
                        tracing::error!("failed to append invocation log file");
                    }
                }
                Err(_) => tracing::error!("invocation log lock poisoned"),
            }
        }
    }
}

pub struct CallLog<'a> {
    fields: Value,
    start: Instant,
    finished: bool,
    owner: &'a InvocationLog,
}
impl CallLog<'_> {
    pub fn finish(mut self, count: usize, size: usize, data: &Value, error: Option<&str>) {
        self.emit(count, size, Some(data), error);
        self.finished = true;
    }
    fn emit(&self, count: usize, size: usize, data: Option<&Value>, error: Option<&str>) {
        let mut record = self.fields.clone();
        record["event"] = json!("tool_end");
        record["timestamp_ms"] = json!(now_ms());
        record["latency_ms"] = json!(self.start.elapsed().as_secs_f64() * 1000.0);
        record["result_count"] = json!(count);
        record["response_bytes"] = json!(size);
        record["error"] = json!(error);
        if let Some(data) = data {
            record["retrieval_bytes"] = json!(serde_json::to_vec(data).map_or(0, |b| b.len()));
            record["coverage"] = data.get("coverage").cloned().unwrap_or(Value::Null);
            record["backend"] = data.get("backend").cloned().unwrap_or(Value::Null);
            record["index_note"] = data.get("index_note").cloned().unwrap_or(Value::Null);
            let locations: Vec<_> = data.get("results").and_then(Value::as_array).into_iter().flatten()
                .map(|r| json!({"path":r["path"], "line":r.get("line").or_else(|| r.get("start_line")), "end_line":r["end_line"], "resolution":r["resolution"]})).collect();
            record["locations"] = json!(locations);
            if data.get("lines").is_some() {
                record["locations"] = json!([{"path":data["path"], "line":data["lines"][0]["line"],
                    "end_line":data["lines"].as_array().and_then(|lines| lines.last()).map(|l| &l["line"])}]);
            }
        }
        self.owner.write_record(&record);
    }
}
impl Drop for CallLog<'_> {
    fn drop(&mut self) {
        if !self.finished {
            self.emit(0, 0, None, Some("cancelled_or_dropped"));
        }
    }
}
pub fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn appends_structured_events_and_tracks_dropped_calls() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("events.jsonl");
        let log = InvocationLog::new("B".into(), Some("trial".into()), Some(&path)).unwrap();
        log.start("find_symbol", &json!({"name":"parse"}), &json!(7))
            .finish(
                1,
                300,
                &json!({"coverage":{"complete":false},"results":[{"path":"src/a.rs","line":3}]}),
                None,
            );
        drop(log.start("read_source", &json!({"path":"src/a.rs"}), &json!(8)));
        let text = std::fs::read_to_string(path).unwrap();
        let events: Vec<Value> = text
            .lines()
            .map(|s| serde_json::from_str(s).unwrap())
            .collect();
        assert_eq!(events.len(), 4);
        assert_eq!(events[1]["locations"][0]["line"], 3);
        assert_eq!(events[1]["coverage"]["complete"], false);
        assert_eq!(events[3]["error"], "cancelled_or_dropped");
        assert_eq!(events[3]["sequence"], 2);
    }
}
