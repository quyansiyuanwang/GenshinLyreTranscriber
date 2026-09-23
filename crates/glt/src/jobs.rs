//! JSONL worker process control with bounded diagnostics and cancellation.

use std::ffi::OsString;
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex, mpsc};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use thiserror::Error;

pub const PROTOCOL_VERSION: u8 = 1;
pub const MAX_PROTOCOL_LINE_BYTES: usize = 1024 * 1024;
pub const DEFAULT_READY_TIMEOUT: Duration = Duration::from_secs(15);
pub const DEFAULT_TERMINAL_TIMEOUT: Duration = Duration::from_secs(24 * 60 * 60);
pub const DEFAULT_CANCEL_GRACE: Duration = Duration::from_secs(5);
pub const MAX_STDERR_BYTES: usize = 64 * 1024;

#[derive(Debug, Error)]
pub enum WorkerError {
    #[error("worker I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("worker JSON is invalid: {0}")]
    Json(#[from] serde_json::Error),
    #[error("worker protocol error [{code}]: {message}")]
    Protocol { code: &'static str, message: String },
    #[error("worker timed out while waiting for {operation}")]
    Timeout { operation: &'static str },
    #[error("worker exited before a terminal message; stderr: {stderr}")]
    Exited { stderr: String },
}

impl WorkerError {
    fn protocol(code: &'static str, message: impl Into<String>) -> Self {
        Self::Protocol {
            code,
            message: message.into(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct WorkerSpec {
    pub program: PathBuf,
    pub args: Vec<OsString>,
    pub working_directory: Option<PathBuf>,
}

impl WorkerSpec {
    pub fn new(program: impl Into<PathBuf>) -> Self {
        Self {
            program: program.into(),
            args: Vec::new(),
            working_directory: None,
        }
    }

    pub fn with_args<I, S>(mut self, args: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<OsString>,
    {
        self.args.extend(args.into_iter().map(Into::into));
        self
    }

    pub fn with_working_directory(mut self, directory: impl Into<PathBuf>) -> Self {
        self.working_directory = Some(directory.into());
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReadyInfo {
    pub worker_version: String,
    pub application_version: String,
    pub model_version: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Operation {
    Transcribe,
    ConvertMidi,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Timing {
    Auto,
    Preserve,
    Straight,
    Triplet,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Transpose {
    Auto(String),
    Semitones(i32),
}

impl Transpose {
    pub fn automatic() -> Self {
        Self::Auto("auto".to_owned())
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartOptions {
    pub timing: Option<Timing>,
    pub bpm: Option<f64>,
    pub transpose: Option<Transpose>,
    pub audio_track: Option<u32>,
    pub start_us: Option<u64>,
    pub end_us: Option<u64>,
    pub preview_wav: Option<bool>,
    pub overwrite: Option<bool>,
    pub mapping_profile: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartRequest {
    pub operation: Operation,
    pub input_path: PathBuf,
    pub staging_dir: PathBuf,
    pub options: StartOptions,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Artifact {
    pub kind: String,
    pub relative_path: String,
    pub sha256: String,
    pub size_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResultPayload {
    pub output_dir: PathBuf,
    pub report_path: PathBuf,
    pub artifacts: Vec<Artifact>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FailurePayload {
    pub code: String,
    pub message: String,
    pub retryable: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub enum WorkerEvent {
    Progress {
        stage: String,
        fraction: Option<f64>,
    },
    Warning {
        code: String,
        message: String,
    },
    Result(ResultPayload),
    Error(FailurePayload),
    Cancelled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CancellationOutcome {
    AlreadyTerminal,
    WorkerCancelled,
    Forced,
}

#[derive(Debug, Deserialize)]
struct Envelope {
    protocol_version: u8,
    job_id: Option<String>,
    #[serde(rename = "type")]
    kind: String,
    payload: Value,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProgressPayload {
    stage: String,
    fraction: Option<f64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WarningPayload {
    code: String,
    message: String,
}

pub struct WorkerClient {
    child: Child,
    stdin: Option<ChildStdin>,
    receiver: mpsc::Receiver<Result<Envelope, WorkerError>>,
    reader_thread: Option<JoinHandle<()>>,
    stderr_thread: Option<JoinHandle<()>>,
    stderr: Arc<Mutex<Vec<u8>>>,
    job_id: Option<String>,
    terminal_seen: bool,
    ready: ReadyInfo,
}

impl WorkerClient {
    pub fn launch(spec: WorkerSpec, ready_timeout: Duration) -> Result<Self, WorkerError> {
        let mut command = Command::new(&spec.program);
        command
            .args(&spec.args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some(directory) = spec.working_directory {
            command.current_dir(directory);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;

            const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
        }
        let mut child = command.spawn()?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| WorkerError::protocol("INTERNAL", "worker stdin was not piped"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| WorkerError::protocol("INTERNAL", "worker stdout was not piped"))?;
        let worker_stderr = child
            .stderr
            .take()
            .ok_or_else(|| WorkerError::protocol("INTERNAL", "worker stderr was not piped"))?;
        let (sender, receiver) = mpsc::channel();
        let reader_thread = thread::spawn(move || read_worker_stdout(stdout, sender));
        let stderr = Arc::new(Mutex::new(Vec::new()));
        let stderr_thread = thread::spawn({
            let stderr = Arc::clone(&stderr);
            move || read_worker_stderr(worker_stderr, stderr)
        });
        let mut client = Self {
            child,
            stdin: Some(stdin),
            receiver,
            reader_thread: Some(reader_thread),
            stderr_thread: Some(stderr_thread),
            stderr,
            job_id: None,
            terminal_seen: false,
            ready: ReadyInfo {
                worker_version: String::new(),
                application_version: String::new(),
                model_version: String::new(),
            },
        };
        let envelope = client.receive_envelope(ready_timeout, "ready")?;
        if envelope.kind != "ready" || envelope.job_id.is_some() {
            return Err(WorkerError::protocol(
                "EXPECTED_READY",
                "worker must send a ready message with null job_id before any job",
            ));
        }
        let ready: ReadyInfo = serde_json::from_value(envelope.payload)?;
        client.ready = ready;
        Ok(client)
    }

    pub fn ready(&self) -> &ReadyInfo {
        &self.ready
    }

    pub fn start(
        &mut self,
        job_id: impl Into<String>,
        request: &StartRequest,
    ) -> Result<(), WorkerError> {
        if self.terminal_seen || self.job_id.is_some() {
            return Err(WorkerError::protocol(
                "INVALID_STATE",
                "worker already has an active or completed job",
            ));
        }
        let job_id = job_id.into();
        if job_id.is_empty() {
            return Err(WorkerError::protocol(
                "INVALID_JOB_ID",
                "job_id cannot be empty",
            ));
        }
        self.send(json!({
            "protocol_version": PROTOCOL_VERSION,
            "job_id": job_id,
            "type": "start",
            "payload": request,
        }))?;
        self.job_id = Some(job_id);
        Ok(())
    }

    pub fn recv_event(&mut self, timeout: Duration) -> Result<WorkerEvent, WorkerError> {
        if self.terminal_seen {
            return Err(WorkerError::protocol(
                "DUPLICATE_TERMINAL",
                "worker emitted more than one terminal message",
            ));
        }
        let envelope = self.receive_envelope(timeout, "job event")?;
        self.validate_job_envelope(&envelope)?;
        match envelope.kind.as_str() {
            "progress" => {
                let payload: ProgressPayload = serde_json::from_value(envelope.payload)?;
                if !is_known_stage(&payload.stage)
                    || payload
                        .fraction
                        .is_some_and(|value| !(0.0..=1.0).contains(&value))
                {
                    return Err(WorkerError::protocol(
                        "SCHEMA_INVALID",
                        "progress payload is invalid",
                    ));
                }
                Ok(WorkerEvent::Progress {
                    stage: payload.stage,
                    fraction: payload.fraction,
                })
            }
            "warning" => {
                let payload: WarningPayload = serde_json::from_value(envelope.payload)?;
                validate_code(&payload.code)?;
                Ok(WorkerEvent::Warning {
                    code: payload.code,
                    message: payload.message,
                })
            }
            "result" => {
                let payload: ResultPayload = serde_json::from_value(envelope.payload)?;
                self.validate_result_paths(&payload)?;
                self.terminal_seen = true;
                Ok(WorkerEvent::Result(payload))
            }
            "error" => {
                let payload: FailurePayload = serde_json::from_value(envelope.payload)?;
                validate_code(&payload.code)?;
                self.terminal_seen = true;
                Ok(WorkerEvent::Error(payload))
            }
            "cancelled" => {
                self.terminal_seen = true;
                Ok(WorkerEvent::Cancelled)
            }
            other => Err(WorkerError::protocol(
                "UNKNOWN_MESSAGE",
                format!("unexpected worker message type: {other}"),
            )),
        }
    }

    pub fn cancel(&mut self, grace: Duration) -> Result<CancellationOutcome, WorkerError> {
        if self.terminal_seen {
            return Ok(CancellationOutcome::AlreadyTerminal);
        }
        let Some(job_id) = self.job_id.as_ref() else {
            return Err(WorkerError::protocol(
                "INVALID_STATE",
                "cannot cancel before a job starts",
            ));
        };
        self.send(json!({
            "protocol_version": PROTOCOL_VERSION,
            "job_id": job_id,
            "type": "cancel",
            "payload": {},
        }))?;
        let deadline = Instant::now() + grace;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                self.terminate_process_tree()?;
                self.terminal_seen = true;
                return Ok(CancellationOutcome::Forced);
            }
            match self.receive_envelope(remaining, "cancellation") {
                Ok(envelope) => {
                    self.validate_job_envelope(&envelope)?;
                    match envelope.kind.as_str() {
                        "cancelled" => {
                            self.terminal_seen = true;
                            if !self.wait_for_exit(remaining)? {
                                self.terminate_process_tree()?;
                                return Ok(CancellationOutcome::Forced);
                            }
                            return Ok(CancellationOutcome::WorkerCancelled);
                        }
                        "error" | "result" => {
                            self.terminal_seen = true;
                            return Ok(CancellationOutcome::AlreadyTerminal);
                        }
                        "progress" | "warning" => continue,
                        other => {
                            return Err(WorkerError::protocol(
                                "UNKNOWN_MESSAGE",
                                format!("unexpected cancellation message: {other}"),
                            ));
                        }
                    }
                }
                Err(WorkerError::Exited { .. } | WorkerError::Timeout { .. }) => {
                    self.terminate_process_tree()?;
                    self.terminal_seen = true;
                    return Ok(CancellationOutcome::Forced);
                }
                Err(error) => return Err(error),
            }
        }
    }

    pub fn is_running(&mut self) -> Result<bool, WorkerError> {
        Ok(self.child.try_wait()?.is_none())
    }

    fn wait_for_exit(&mut self, timeout: Duration) -> Result<bool, WorkerError> {
        let deadline = Instant::now() + timeout;
        loop {
            if self.child.try_wait()?.is_some() {
                return Ok(true);
            }
            if Instant::now() >= deadline {
                return Ok(false);
            }
            thread::sleep(Duration::from_millis(10));
        }
    }

    pub fn stderr_text(&self) -> String {
        let bytes = self
            .stderr
            .lock()
            .map_or_else(|_| Vec::new(), |value| value.clone());
        String::from_utf8_lossy(&bytes).into_owned()
    }

    fn validate_job_envelope(&self, envelope: &Envelope) -> Result<(), WorkerError> {
        let expected = self
            .job_id
            .as_deref()
            .ok_or_else(|| WorkerError::protocol("INVALID_STATE", "no active job"))?;
        if envelope.job_id.as_deref() != Some(expected) {
            return Err(WorkerError::protocol(
                "JOB_MISMATCH",
                "worker response does not match the active job",
            ));
        }
        Ok(())
    }

    fn validate_result_paths(&self, payload: &ResultPayload) -> Result<(), WorkerError> {
        for artifact in &payload.artifacts {
            safe_relative_path(&artifact.relative_path)?;
            safe_join(&payload.output_dir, &artifact.relative_path)?;
            if artifact.sha256.len() != 64
                || !artifact.sha256.bytes().all(|byte| byte.is_ascii_hexdigit())
            {
                return Err(WorkerError::protocol(
                    "SCHEMA_INVALID",
                    "artifact sha256 must contain 64 hexadecimal characters",
                ));
            }
        }
        if payload.report_path.is_absolute() || has_parent_component(&payload.report_path) {
            return Err(WorkerError::protocol(
                "UNSAFE_PATH",
                "report path must be relative to output_dir",
            ));
        }
        Ok(())
    }

    fn send(&mut self, value: Value) -> Result<(), WorkerError> {
        let stdin = self
            .stdin
            .as_mut()
            .ok_or_else(|| WorkerError::protocol("INVALID_STATE", "worker stdin is closed"))?;
        let mut writer = BufWriter::new(stdin);
        serde_json::to_writer(&mut writer, &value)?;
        writer.write_all(b"\n")?;
        writer.flush()?;
        Ok(())
    }

    fn receive_envelope(
        &mut self,
        timeout: Duration,
        operation: &'static str,
    ) -> Result<Envelope, WorkerError> {
        let deadline = Instant::now() + timeout;
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(WorkerError::Timeout { operation });
        }
        match self.receiver.recv_timeout(remaining) {
            Ok(Ok(envelope)) => {
                if envelope.protocol_version != PROTOCOL_VERSION {
                    return Err(WorkerError::protocol(
                        "UNSUPPORTED_VERSION",
                        format!("unsupported protocol version {}", envelope.protocol_version),
                    ));
                }
                Ok(envelope)
            }
            Ok(Err(error)) => Err(error),
            Err(mpsc::RecvTimeoutError::Timeout) => Err(WorkerError::Timeout { operation }),
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(self.worker_exited_error()),
        }
    }

    fn worker_exited_error(&mut self) -> WorkerError {
        match self.child.try_wait() {
            Ok(Some(status)) => WorkerError::Exited {
                stderr: format_exit_status(status, &self.stderr_text()),
            },
            Ok(None) => WorkerError::Exited {
                stderr: self.stderr_text(),
            },
            Err(error) => WorkerError::Io(error),
        }
    }

    fn terminate_process_tree(&mut self) -> Result<(), WorkerError> {
        self.stdin.take();
        if self.child.try_wait()?.is_some() {
            return Ok(());
        }
        #[cfg(windows)]
        {
            let pid = self.child.id().to_string();
            let status = Command::new("taskkill")
                .args(["/PID", pid.as_str(), "/T", "/F"])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
            if status.is_err() || !status.is_ok_and(|value| value.success()) {
                self.child.kill()?;
            }
        }
        #[cfg(not(windows))]
        {
            self.child.kill()?;
        }
        self.child.wait()?;
        Ok(())
    }
}

impl Drop for WorkerClient {
    fn drop(&mut self) {
        let _ = self.terminate_process_tree();
        self.stdin.take();
        if let Some(thread) = self.reader_thread.take() {
            let _ = thread.join();
        }
        if let Some(thread) = self.stderr_thread.take() {
            let _ = thread.join();
        }
    }
}

fn read_worker_stdout(stdout: ChildStdout, sender: mpsc::Sender<Result<Envelope, WorkerError>>) {
    let mut reader = BufReader::new(stdout);
    loop {
        match read_bounded_line(&mut reader) {
            Ok(Some(line)) => match serde_json::from_str::<Envelope>(&line) {
                Ok(envelope) => {
                    if sender.send(Ok(envelope)).is_err() {
                        return;
                    }
                }
                Err(error) => {
                    let _ = sender.send(Err(WorkerError::Json(error)));
                    return;
                }
            },
            Ok(None) => return,
            Err(error) => {
                let _ = sender.send(Err(error));
                return;
            }
        }
    }
}

fn read_bounded_line(reader: &mut BufReader<ChildStdout>) -> Result<Option<String>, WorkerError> {
    let mut bytes = Vec::new();
    let mut limited = reader.take((MAX_PROTOCOL_LINE_BYTES + 1) as u64);
    let count = limited.read_until(b'\n', &mut bytes)?;
    if count == 0 {
        return Ok(None);
    }
    if bytes.len() > MAX_PROTOCOL_LINE_BYTES {
        return Err(WorkerError::protocol(
            "LINE_TOO_LONG",
            "worker stdout line exceeds 1 MiB",
        ));
    }
    let line = String::from_utf8(bytes)
        .map_err(|_| WorkerError::protocol("INVALID_UTF8", "worker stdout must be valid UTF-8"))?;
    Ok(Some(line))
}

fn read_worker_stderr(mut stderr: impl Read, destination: Arc<Mutex<Vec<u8>>>) {
    let mut buffer = [0_u8; 8192];
    loop {
        match stderr.read(&mut buffer) {
            Ok(0) | Err(_) => return,
            Ok(count) => {
                if let Ok(mut stored) = destination.lock() {
                    let remaining = MAX_STDERR_BYTES.saturating_sub(stored.len());
                    stored.extend_from_slice(&buffer[..count.min(remaining)]);
                }
            }
        }
    }
}

fn safe_relative_path(path: &str) -> Result<PathBuf, WorkerError> {
    let path = PathBuf::from(path);
    if path.is_absolute() || has_parent_component(&path) {
        return Err(WorkerError::protocol(
            "UNSAFE_PATH",
            "artifact path must be relative and cannot escape output_dir",
        ));
    }
    Ok(path)
}

pub fn safe_join(root: &Path, relative: &str) -> Result<PathBuf, WorkerError> {
    let relative = safe_relative_path(relative)?;
    if relative.as_os_str().is_empty() {
        return Err(WorkerError::protocol(
            "UNSAFE_PATH",
            "artifact path cannot be empty",
        ));
    }
    Ok(root.join(relative))
}

fn has_parent_component(path: &Path) -> bool {
    path.components().any(|component| {
        matches!(
            component,
            Component::ParentDir | Component::RootDir | Component::Prefix(_)
        )
    })
}

fn format_exit_status(status: std::process::ExitStatus, stderr: &str) -> String {
    let code = status.code().map_or_else(
        || "terminated by signal".to_owned(),
        |value| value.to_string(),
    );
    if stderr.is_empty() {
        format!("exit code {code}")
    } else {
        format!("exit code {code}; stderr: {stderr}")
    }
}

fn is_known_stage(stage: &str) -> bool {
    matches!(
        stage,
        "validating"
            | "extracting"
            | "importing"
            | "transcribing"
            | "cleaning"
            | "analyzing"
            | "quantizing"
            | "mapping"
            | "exporting"
            | "completed"
    )
}

fn validate_code(code: &str) -> Result<(), WorkerError> {
    let mut characters = code.chars();
    let valid_first = characters
        .next()
        .is_some_and(|character| character.is_ascii_uppercase());
    let valid_rest = characters.all(|character| {
        character.is_ascii_uppercase() || character.is_ascii_digit() || character == '_'
    });
    if !valid_first || !valid_rest || code.len() > 64 {
        return Err(WorkerError::protocol(
            "SCHEMA_INVALID",
            "worker code must use uppercase ASCII, digits and underscores",
        ));
    }
    Ok(())
}
