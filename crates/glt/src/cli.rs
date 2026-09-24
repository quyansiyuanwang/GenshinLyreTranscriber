//! Command-line parsing and worker job lifecycle.

use std::ffi::OsString;
use std::fs;
use std::path::PathBuf;
use std::str::FromStr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use clap::{Parser, Subcommand, ValueEnum};
use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::jobs::{
    DEFAULT_CANCEL_GRACE, DEFAULT_READY_TIMEOUT, DEFAULT_TERMINAL_TIMEOUT, Operation,
    ResultPayload, StartOptions, StartRequest, Timing, Transpose, WorkerClient, WorkerError,
    WorkerEvent, WorkerSpec,
};

const EXIT_USAGE: i32 = 2;
const EXIT_ENVIRONMENT: i32 = 3;
const EXIT_PROCESSING: i32 = 4;
const EXIT_OUTPUT: i32 = 5;
const EXIT_CANCELLED: i32 = 130;

#[derive(Debug, Parser)]
#[command(
    name = "glt",
    version,
    about = "Offline Genshin Lyre transcription CLI"
)]
pub struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Start the terminal user interface.
    Tui,
    /// Check bundled worker and model resources.
    Doctor(DoctorArgs),
    /// Transcribe a local audio or video input.
    Transcribe(TranscribeArgs),
    /// Convert a local MIDI file through the same mapping pipeline.
    ConvertMidi(ConvertMidiArgs),
    /// Preview a completed result directory.
    Preview(PreviewArgs),
}

#[derive(Debug, clap::Args)]
struct DoctorArgs {
    /// Override the worker executable path.
    #[arg(long)]
    worker: Option<PathBuf>,
    /// Emit a machine-readable result.
    #[arg(long)]
    json: bool,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum TimingArg {
    Auto,
    Preserve,
    Straight,
    Triplet,
}

impl From<TimingArg> for Timing {
    fn from(value: TimingArg) -> Self {
        match value {
            TimingArg::Auto => Self::Auto,
            TimingArg::Preserve => Self::Preserve,
            TimingArg::Straight => Self::Straight,
            TimingArg::Triplet => Self::Triplet,
        }
    }
}

#[derive(Debug, Clone)]
enum TransposeArg {
    Auto,
    Semitones(i32),
}

impl FromStr for TransposeArg {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        if value == "auto" {
            return Ok(Self::Auto);
        }
        let semitones = value
            .parse::<i32>()
            .map_err(|_| "transpose must be 'auto' or an integer".to_owned())?;
        if !(-48..=48).contains(&semitones) {
            return Err("transpose must be in range -48..48".to_owned());
        }
        Ok(Self::Semitones(semitones))
    }
}

impl From<TransposeArg> for Transpose {
    fn from(value: TransposeArg) -> Self {
        match value {
            TransposeArg::Auto => Self::automatic(),
            TransposeArg::Semitones(value) => Self::Semitones(value),
        }
    }
}

#[derive(Debug, clap::Args)]
struct TranscribeArgs {
    /// Local audio or video path.
    input: PathBuf,
    /// Destination result directory.
    #[arg(long)]
    output: PathBuf,
    /// Timing strategy.
    #[arg(long, value_enum, default_value = "auto")]
    timing: TimingArg,
    /// Explicit BPM override.
    #[arg(long)]
    bpm: Option<f64>,
    /// Transpose selection: auto or semitones.
    #[arg(long, default_value = "auto")]
    transpose: TransposeArg,
    /// Zero-based media audio track.
    #[arg(long)]
    audio_track: Option<u32>,
    /// Segment start in seconds.
    #[arg(long)]
    start_seconds: Option<f64>,
    /// Segment end in seconds.
    #[arg(long)]
    end_seconds: Option<f64>,
    /// Request a synthesized preview WAV.
    #[arg(long)]
    preview_wav: bool,
    /// Allow replacing files previously created by this tool.
    #[arg(long)]
    overwrite: bool,
    /// Emit a machine-readable result.
    #[arg(long)]
    json: bool,
    /// Override the worker executable path.
    #[arg(long)]
    worker: Option<PathBuf>,
}

#[derive(Debug, clap::Args)]
struct ConvertMidiArgs {
    /// Local MIDI path.
    input: PathBuf,
    /// Destination result directory.
    #[arg(long)]
    output: PathBuf,
    /// Timing strategy.
    #[arg(long, value_enum, default_value = "preserve")]
    timing: TimingArg,
    /// Transpose selection: auto or semitones.
    #[arg(long, default_value = "auto")]
    transpose: TransposeArg,
    /// Request a synthesized preview WAV.
    #[arg(long)]
    preview_wav: bool,
    /// Allow replacing files previously created by this tool.
    #[arg(long)]
    overwrite: bool,
    /// Emit a machine-readable result.
    #[arg(long)]
    json: bool,
    /// Override the worker executable path.
    #[arg(long)]
    worker: Option<PathBuf>,
}

#[derive(Debug, clap::Args)]
struct PreviewArgs {
    /// Result directory to preview.
    result_dir: PathBuf,
}

#[derive(Debug, Error)]
pub(crate) enum CliError {
    #[error("invalid argument: {0}")]
    InvalidArgument(String),
    #[error("worker was not found at {0}")]
    WorkerNotFound(PathBuf),
    #[error("worker failed: {0}")]
    WorkerFailure(String),
    #[error("worker protocol failed: {0}")]
    Worker(#[from] WorkerError),
    #[error("output operation failed: {0}")]
    Output(#[from] std::io::Error),
    #[error("job cancelled by user")]
    Cancelled,
    #[error("feature is not implemented yet: {0}")]
    NotImplemented(&'static str),
}

impl CliError {
    fn exit_code(&self) -> i32 {
        match self {
            Self::InvalidArgument(_) => EXIT_USAGE,
            Self::WorkerNotFound(_) => EXIT_ENVIRONMENT,
            Self::Worker(_) | Self::WorkerFailure(_) => EXIT_PROCESSING,
            Self::Output(_) => EXIT_OUTPUT,
            Self::Cancelled => EXIT_CANCELLED,
            Self::NotImplemented(_) => EXIT_ENVIRONMENT,
        }
    }
}

pub fn run() -> i32 {
    let cli = match Cli::try_parse() {
        Ok(cli) => cli,
        Err(error) => {
            let code = error.exit_code();
            let _ = error.print();
            return code;
        }
    };
    match execute(cli) {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("{error}");
            error.exit_code()
        }
    }
}

fn execute(cli: Cli) -> Result<(), CliError> {
    match cli.command {
        Some(Command::Doctor(args)) => run_doctor(args),
        Some(Command::Transcribe(args)) => run_transcribe(args),
        Some(Command::ConvertMidi(args)) => run_convert_midi(args),
        Some(Command::Preview(_)) => Err(CliError::NotImplemented("preview")),
        Some(Command::Tui) | None => crate::tui::run(),
    }
}

fn run_doctor(args: DoctorArgs) -> Result<(), CliError> {
    let spec = resolve_worker_spec(args.worker)?;
    let client = WorkerClient::launch(spec, DEFAULT_READY_TIMEOUT)?;
    let ready = client.ready();
    if args.json {
        println!(
            "{}",
            serde_json::to_string(&serde_json::json!({
                "status": "ok",
                "worker_version": ready.worker_version,
                "application_version": ready.application_version,
                "model_version": ready.model_version,
            }))
            .expect("JSON serialization cannot fail")
        );
    } else {
        println!("worker: {}", ready.worker_version);
        println!("application: {}", ready.application_version);
        println!("model: {}", ready.model_version);
    }
    Ok(())
}

fn run_transcribe(args: TranscribeArgs) -> Result<(), CliError> {
    let options = build_options(
        args.timing,
        args.bpm,
        args.transpose,
        args.audio_track,
        args.start_seconds,
        args.end_seconds,
        args.preview_wav,
        args.overwrite,
    )?;
    run_job(
        args.worker,
        args.input,
        args.output,
        Operation::Transcribe,
        options,
        args.json,
    )
}

fn run_convert_midi(args: ConvertMidiArgs) -> Result<(), CliError> {
    let options = build_options(
        args.timing,
        None,
        args.transpose,
        None,
        None,
        None,
        args.preview_wav,
        args.overwrite,
    )?;
    run_job(
        args.worker,
        args.input,
        args.output,
        Operation::ConvertMidi,
        options,
        args.json,
    )
}

#[allow(clippy::too_many_arguments)]
fn build_options(
    timing: TimingArg,
    bpm: Option<f64>,
    transpose: TransposeArg,
    audio_track: Option<u32>,
    start_seconds: Option<f64>,
    end_seconds: Option<f64>,
    preview_wav: bool,
    overwrite: bool,
) -> Result<StartOptions, CliError> {
    build_job_options(
        timing.into(),
        bpm,
        transpose.into(),
        audio_track,
        start_seconds,
        end_seconds,
        preview_wav,
        overwrite,
    )
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn build_job_options(
    timing: Timing,
    bpm: Option<f64>,
    transpose: Transpose,
    audio_track: Option<u32>,
    start_seconds: Option<f64>,
    end_seconds: Option<f64>,
    preview_wav: bool,
    overwrite: bool,
) -> Result<StartOptions, CliError> {
    if bpm.is_some_and(|value| !value.is_finite() || value <= 0.0 || value > 1000.0) {
        return Err(CliError::InvalidArgument(
            "bpm must be finite and in range (0, 1000]".to_owned(),
        ));
    }
    let start_us = seconds_to_microseconds(start_seconds, "start-seconds")?;
    let end_us = seconds_to_microseconds(end_seconds, "end-seconds")?;
    if start_us
        .zip(end_us)
        .is_some_and(|(start, end)| end <= start)
    {
        return Err(CliError::InvalidArgument(
            "end-seconds must be greater than start-seconds".to_owned(),
        ));
    }
    Ok(StartOptions {
        timing: Some(timing),
        bpm,
        transpose: Some(transpose),
        audio_track,
        start_us,
        end_us,
        preview_wav: Some(preview_wav),
        overwrite: Some(overwrite),
        mapping_profile: None,
    })
}

fn seconds_to_microseconds(value: Option<f64>, name: &str) -> Result<Option<u64>, CliError> {
    let Some(value) = value else {
        return Ok(None);
    };
    if !value.is_finite() || value < 0.0 {
        return Err(CliError::InvalidArgument(format!(
            "{name} must be a finite non-negative number"
        )));
    }
    let microseconds = value * 1_000_000.0;
    if microseconds > 9_007_199_254_740_991.0 {
        return Err(CliError::InvalidArgument(format!("{name} is out of range")));
    }
    Ok(Some(microseconds.round() as u64))
}

#[derive(Debug)]
pub(crate) struct JobOutcome {
    pub job_id: String,
    pub result: ResultPayload,
}

#[derive(Debug, Clone)]
pub(crate) enum JobUpdate {
    Progress {
        stage: String,
        fraction: Option<f64>,
    },
    Warning {
        code: String,
        message: String,
    },
}

fn run_job(
    worker_override: Option<PathBuf>,
    input: PathBuf,
    output: PathBuf,
    operation: Operation,
    options: StartOptions,
    json_output: bool,
) -> Result<(), CliError> {
    let cancellation = cancellation_flag()?;
    let outcome = run_job_with_cancel(
        worker_override,
        input,
        output,
        operation,
        options,
        cancellation,
        |update| match update {
            JobUpdate::Progress { stage, fraction } => match fraction {
                Some(value) => eprintln!("{stage}: {:.0}%", value * 100.0),
                None => eprintln!("{stage}: ..."),
            },
            JobUpdate::Warning { code, message } => {
                eprintln!("warning [{code}]: {message}");
            }
        },
    )?;
    let result = outcome.result;
    if json_output {
        println!(
            "{}",
            serde_json::to_string(&serde_json::json!({
                "job_id": outcome.job_id,
                "result": result,
            }))
            .expect("JSON serialization cannot fail")
        );
    } else {
        println!("result: {}", result.output_dir.display());
        println!("report: {}", result.report_path.display());
    }
    Ok(())
}

pub(crate) fn run_job_with_cancel<F>(
    worker_override: Option<PathBuf>,
    input: PathBuf,
    output: PathBuf,
    operation: Operation,
    options: StartOptions,
    cancellation: Arc<AtomicBool>,
    mut on_update: F,
) -> Result<JobOutcome, CliError>
where
    F: FnMut(JobUpdate),
{
    let input = absolute_path(&input)?;
    if !input.is_file() {
        return Err(CliError::InvalidArgument(format!(
            "input file does not exist: {}",
            input.display()
        )));
    }
    let job_id = generate_job_id();
    let output = absolute_path(&output)?;
    let overwrite = options.overwrite.unwrap_or(false);
    if output.exists() && !overwrite {
        return Err(CliError::Output(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            format!("output already exists: {}", output.display()),
        )));
    }
    let output_parent = output.parent().ok_or_else(|| {
        CliError::InvalidArgument("output must have a parent directory".to_owned())
    })?;
    fs::create_dir_all(output_parent)?;
    if output.exists() && overwrite && input.starts_with(&output) {
        return Err(CliError::InvalidArgument(
            "input file must not be inside an output directory being overwritten".to_owned(),
        ));
    }
    let staging_dir = output_parent.join(format!(".glt-job-{job_id}"));
    fs::create_dir(&staging_dir)?;
    let request = StartRequest {
        operation,
        input_path: input,
        staging_dir: staging_dir.clone(),
        options,
    };
    let spec = resolve_worker_spec(worker_override)?;
    let mut client = WorkerClient::launch(spec, DEFAULT_READY_TIMEOUT)?;
    client.start(job_id.clone(), &request)?;
    let started = Instant::now();
    loop {
        if cancellation.load(Ordering::Relaxed) {
            let _ = client.cancel(DEFAULT_CANCEL_GRACE)?;
            return Err(CliError::Cancelled);
        }
        if started.elapsed() > DEFAULT_TERMINAL_TIMEOUT {
            return Err(CliError::Worker(WorkerError::Timeout {
                operation: "job terminal message",
            }));
        }
        match client.recv_event(Duration::from_millis(100)) {
            Ok(WorkerEvent::Progress { stage, fraction }) => {
                on_update(JobUpdate::Progress { stage, fraction });
            }
            Ok(WorkerEvent::Warning { code, message, .. }) => {
                on_update(JobUpdate::Warning { code, message });
            }
            Ok(WorkerEvent::Result(result)) => {
                let result = publish_result(&staging_dir, &output, overwrite, result)?;
                return Ok(JobOutcome { job_id, result });
            }
            Ok(WorkerEvent::Error(error)) => {
                return Err(CliError::WorkerFailure(format!(
                    "[{}] {} (retryable={})",
                    error.code, error.message, error.retryable
                )));
            }
            Ok(WorkerEvent::Cancelled) => return Err(CliError::Cancelled),
            Err(WorkerError::Timeout { .. }) => continue,
            Err(error) => return Err(error.into()),
        }
    }
}

fn absolute_path(path: &std::path::Path) -> Result<PathBuf, CliError> {
    if path.is_absolute() {
        Ok(path.to_path_buf())
    } else {
        Ok(std::env::current_dir()?.join(path))
    }
}

fn publish_result(
    staging: &std::path::Path,
    output: &std::path::Path,
    overwrite: bool,
    mut result: ResultPayload,
) -> Result<ResultPayload, CliError> {
    if !staging.is_dir() {
        return Err(CliError::Output(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            format!("worker staging directory is missing: {}", staging.display()),
        )));
    }
    let report_relative = result.report_path.to_str().ok_or_else(|| {
        CliError::InvalidArgument("worker report path must be valid UTF-8".to_owned())
    })?;
    let report_source = crate::jobs::safe_join(staging, report_relative)?;
    if !report_source.is_file()
        || !result
            .artifacts
            .iter()
            .any(|artifact| artifact.kind == "report" && artifact.relative_path == report_relative)
    {
        return Err(CliError::Output(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            format!(
                "worker report artifact is missing: {}",
                report_source.display()
            ),
        )));
    }
    for artifact in &result.artifacts {
        let source = crate::jobs::safe_join(staging, &artifact.relative_path)?;
        if !source.is_file() {
            return Err(CliError::Output(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("worker artifact is missing: {}", source.display()),
            )));
        }
        let metadata = source.metadata()?;
        if metadata.len() != artifact.size_bytes {
            return Err(CliError::Output(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("worker artifact size mismatch: {}", source.display()),
            )));
        }
        if sha256_file(&source)? != artifact.sha256 {
            return Err(CliError::Output(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("worker artifact hash mismatch: {}", source.display()),
            )));
        }
    }

    if !output.exists() {
        fs::rename(staging, output)?;
        result.output_dir = output.to_path_buf();
        return Ok(result);
    }
    if !overwrite {
        return Err(CliError::Output(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            format!("output already exists: {}", output.display()),
        )));
    }

    replace_directory(staging, output)?;
    result.output_dir = output.to_path_buf();
    Ok(result)
}

fn replace_directory(staging: &std::path::Path, output: &std::path::Path) -> Result<(), CliError> {
    if !output.is_dir() {
        return Err(CliError::Output(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            format!("output path is not a directory: {}", output.display()),
        )));
    }
    let parent = output.parent().ok_or_else(|| {
        CliError::InvalidArgument("output must have a parent directory".to_owned())
    })?;
    let backup = parent.join(format!(
        ".glt-backup-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    ));
    if backup.exists() {
        return Err(CliError::Output(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            format!("backup path already exists: {}", backup.display()),
        )));
    }
    fs::rename(output, &backup)?;
    if let Err(error) = fs::rename(staging, output) {
        if let Err(restore_error) = fs::rename(&backup, output) {
            return Err(CliError::Output(std::io::Error::other(format!(
                "failed to publish output ({error}) and failed to restore previous output ({restore_error})"
            ))));
        }
        return Err(CliError::Output(error));
    }
    if let Err(error) = fs::remove_dir_all(&backup) {
        eprintln!(
            "warning: published output but could not remove backup {}: {error}",
            backup.display()
        );
    }
    Ok(())
}

fn sha256_file(path: &std::path::Path) -> Result<String, CliError> {
    let mut file = fs::File::open(path)?;
    let mut hasher = Sha256::new();
    std::io::copy(&mut file, &mut hasher)?;
    Ok(format!("{:x}", hasher.finalize()))
}

fn resolve_worker_spec(override_path: Option<PathBuf>) -> Result<WorkerSpec, CliError> {
    if let Some(path) = override_path {
        return checked_worker_spec(path);
    }
    if let Some(path) = std::env::var_os("GLT_WORKER_PATH") {
        return checked_worker_spec(PathBuf::from(path));
    }
    let executable = std::env::current_exe()?;
    let parent = executable
        .parent()
        .ok_or_else(|| CliError::WorkerNotFound(executable.clone()))?;
    let candidates = if cfg!(windows) {
        vec![
            parent.join("glt-worker").join("glt-worker.exe"),
            parent.join("glt-worker.exe"),
        ]
    } else {
        vec![
            parent.join("glt-worker").join("glt-worker"),
            parent.join("glt-worker"),
        ]
    };
    candidates
        .into_iter()
        .find(|path| path.is_file())
        .ok_or_else(|| CliError::WorkerNotFound(parent.join("glt-worker")))
        .and_then(checked_worker_spec)
}

fn checked_worker_spec(path: PathBuf) -> Result<WorkerSpec, CliError> {
    if !path.is_file() {
        return Err(CliError::WorkerNotFound(path));
    }
    if path.extension().is_some_and(|extension| extension == "py") {
        return Ok(
            WorkerSpec::new("python").with_args([OsString::from("-u"), path.into_os_string()])
        );
    }
    Ok(WorkerSpec::new(path))
}

fn generate_job_id() -> String {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_millis());
    format!("local-{}-{timestamp}", std::process::id())
}

fn cancellation_flag() -> Result<Arc<AtomicBool>, CliError> {
    static FLAG: OnceLock<Arc<AtomicBool>> = OnceLock::new();
    if let Some(flag) = FLAG.get() {
        return Ok(Arc::clone(flag));
    }
    let flag = Arc::new(AtomicBool::new(false));
    let handler_flag = Arc::clone(&flag);
    ctrlc::set_handler(move || handler_flag.store(true, Ordering::SeqCst)).map_err(|error| {
        CliError::InvalidArgument(format!("cannot install Ctrl+C handler: {error}"))
    })?;
    let stored = FLAG.get_or_init(|| flag);
    Ok(Arc::clone(stored))
}

pub fn worker_args(
    program: impl Into<PathBuf>,
    args: impl IntoIterator<Item = OsString>,
) -> WorkerSpec {
    WorkerSpec::new(program).with_args(args)
}
