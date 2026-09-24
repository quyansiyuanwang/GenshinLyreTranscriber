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
    DEFAULT_CANCEL_GRACE, DEFAULT_READY_TIMEOUT, DEFAULT_TERMINAL_TIMEOUT, FilterPreset,
    FilterSpec, Operation, ResultPayload, StartOptions, StartRequest, Timing, Transpose,
    WorkerClient, WorkerError, WorkerEvent, WorkerSpec,
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
    /// Re-filter a v2 result from its cached candidate notes.
    Filter(FilterArgs),
    /// Render all player-facing artifacts from an existing performance revision.
    Performance(PerformanceArgs),
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

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
enum FilterPresetArg {
    Off,
    Auto,
    Balanced,
    Melody,
}

impl From<FilterPresetArg> for FilterPreset {
    fn from(value: FilterPresetArg) -> Self {
        match value {
            FilterPresetArg::Off => Self::Off,
            FilterPresetArg::Auto => Self::Auto,
            FilterPresetArg::Balanced => Self::Balanced,
            FilterPresetArg::Melody => Self::Melody,
        }
    }
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

#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
pub(crate) enum CleaningProfile {
    Auto,
    Solo,
    Mix,
    Strict,
}

impl CleaningProfile {
    fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Solo => "solo",
            Self::Mix => "mix",
            Self::Strict => "strict",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct CleaningOptions {
    pub profile: CleaningProfile,
    pub min_confidence: Option<f64>,
    pub min_duration_us: Option<u64>,
    pub retrigger_gap_us: Option<u64>,
}

impl Default for CleaningOptions {
    fn default() -> Self {
        Self {
            profile: CleaningProfile::Auto,
            min_confidence: None,
            min_duration_us: None,
            retrigger_gap_us: None,
        }
    }
}

impl CleaningOptions {
    pub(crate) fn validate(self) -> Result<Self, CliError> {
        if self
            .min_confidence
            .is_some_and(|value| !value.is_finite() || !(0.0..=1.0).contains(&value))
        {
            return Err(CliError::InvalidArgument(
                "min-confidence must be finite and in range 0..=1".to_owned(),
            ));
        }
        if self.min_duration_us.is_some_and(|value| value > 60_000_000) {
            return Err(CliError::InvalidArgument(
                "min-duration-ms must be at most 60000".to_owned(),
            ));
        }
        if self
            .retrigger_gap_us
            .is_some_and(|value| value > 60_000_000)
        {
            return Err(CliError::InvalidArgument(
                "retrigger-gap-ms must be at most 60000".to_owned(),
            ));
        }
        Ok(self)
    }

    pub(crate) fn worker_env(self) -> Vec<(String, String)> {
        let mut env = vec![(
            "GLT_CLEANING_PROFILE".to_owned(),
            self.profile.as_str().to_owned(),
        )];
        if let Some(value) = self.min_confidence {
            env.push(("GLT_MIN_CONFIDENCE".to_owned(), value.to_string()));
        }
        if let Some(value) = self.min_duration_us {
            env.push(("GLT_MIN_DURATION_US".to_owned(), value.to_string()));
        }
        if let Some(value) = self.retrigger_gap_us {
            env.push(("GLT_RETRIGGER_GAP_US".to_owned(), value.to_string()));
        }
        env
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
pub(crate) enum ArrangementProfile {
    Balanced,
    Off,
}

impl ArrangementProfile {
    fn as_str(self) -> &'static str {
        match self {
            Self::Balanced => "balanced",
            Self::Off => "off",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct ArrangementOptions {
    pub profile: ArrangementProfile,
    pub onset_window_us: u64,
    pub max_voices: u8,
}

impl Default for ArrangementOptions {
    fn default() -> Self {
        Self {
            profile: ArrangementProfile::Balanced,
            onset_window_us: 150_000,
            max_voices: 2,
        }
    }
}

impl ArrangementOptions {
    pub(crate) fn validate(self) -> Result<Self, CliError> {
        if !(1_000..=1_000_000).contains(&self.onset_window_us) {
            return Err(CliError::InvalidArgument(
                "onset-window-ms must be in range 1..=1000".to_owned(),
            ));
        }
        if !(1..=21).contains(&self.max_voices) {
            return Err(CliError::InvalidArgument(
                "max-voices must be in range 1..=21".to_owned(),
            ));
        }
        Ok(self)
    }

    pub(crate) fn worker_env(self) -> Vec<(String, String)> {
        vec![
            (
                "GLT_ARRANGEMENT".to_owned(),
                self.profile.as_str().to_owned(),
            ),
            (
                "GLT_ONSET_WINDOW_US".to_owned(),
                self.onset_window_us.to_string(),
            ),
            ("GLT_MAX_VOICES".to_owned(), self.max_voices.to_string()),
        ]
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
    /// Minimum Basic Pitch note confidence.
    #[arg(long)]
    min_confidence: Option<f64>,
    /// Minimum note duration in milliseconds.
    #[arg(long)]
    min_duration_ms: Option<u64>,
    /// Retrigger/overlap merge gap in milliseconds.
    #[arg(long)]
    retrigger_gap_ms: Option<u64>,
    /// Cleaning profile: auto, solo, mix or strict.
    #[arg(long, value_enum, default_value = "auto")]
    cleaning_profile: CleaningProfile,
    /// Playability arrangement: balanced or off.
    #[arg(long, value_enum, default_value = "balanced")]
    arrangement: ArrangementProfile,
    /// Merge onsets separated by at most this many milliseconds.
    #[arg(long, default_value_t = 150)]
    onset_window_ms: u64,
    /// Maximum simultaneous voices kept in one arranged event.
    #[arg(long, default_value_t = 2)]
    max_voices: u8,
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
    /// Minimum Basic Pitch note confidence.
    #[arg(long)]
    min_confidence: Option<f64>,
    /// Minimum note duration in milliseconds.
    #[arg(long)]
    min_duration_ms: Option<u64>,
    /// Retrigger/overlap merge gap in milliseconds.
    #[arg(long)]
    retrigger_gap_ms: Option<u64>,
    /// Cleaning profile: auto, solo, mix or strict.
    #[arg(long, value_enum, default_value = "auto")]
    cleaning_profile: CleaningProfile,
    /// Playability arrangement: balanced or off.
    #[arg(long, value_enum, default_value = "balanced")]
    arrangement: ArrangementProfile,
    /// Merge onsets separated by at most this many milliseconds.
    #[arg(long, default_value_t = 150)]
    onset_window_ms: u64,
    /// Maximum simultaneous voices kept in one arranged event.
    #[arg(long, default_value_t = 2)]
    max_voices: u8,
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
    /// Playback volume in range 0..=1.
    #[arg(long, default_value_t = 1.0)]
    volume: f32,
}

#[derive(Debug, clap::Args)]
#[command(group(
    clap::ArgGroup::new("filter_source")
        .required(true)
        .args(["filter_file", "auto_filter", "preset"])
))]
struct FilterArgs {
    /// Existing v2 result directory.
    result_dir: PathBuf,
    /// New result directory for the filtered variant.
    #[arg(long)]
    output: PathBuf,
    /// JSON file containing one FilterSpec v1 object.
    #[arg(long, conflicts_with_all = ["auto_filter", "preset"])]
    filter_file: Option<PathBuf>,
    /// Automatically derive editable filter rules from the candidate cache.
    #[arg(long = "auto", conflicts_with_all = ["filter_file", "preset"])]
    auto_filter: bool,
    /// Apply a built-in filter preset.
    #[arg(long, value_enum, conflicts_with_all = ["filter_file", "auto_filter"])]
    preset: Option<FilterPresetArg>,
    /// Allow replacing the output directory.
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
struct PerformanceArgs {
    /// Existing result directory containing performance.json.
    result_dir: PathBuf,
    /// New result directory for the derived performance revision.
    #[arg(long)]
    output: PathBuf,
    /// Optional title used in readable text output.
    #[arg(long)]
    title: Option<String>,
    /// Request a synthesized preview WAV.
    #[arg(long)]
    preview_wav: bool,
    /// Allow replacing the output directory.
    #[arg(long)]
    overwrite: bool,
    /// Emit a machine-readable result.
    #[arg(long)]
    json: bool,
    /// Override the worker executable path.
    #[arg(long)]
    worker: Option<PathBuf>,
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
    #[error("environment error: {0}")]
    Environment(String),
    #[error("output operation failed: {0}")]
    Output(#[from] std::io::Error),
    #[error("job cancelled by user")]
    Cancelled,
}

impl CliError {
    fn exit_code(&self) -> i32 {
        match self {
            Self::InvalidArgument(_) => EXIT_USAGE,
            Self::WorkerNotFound(_) | Self::Environment(_) => EXIT_ENVIRONMENT,
            Self::Worker(_) | Self::WorkerFailure(_) => EXIT_PROCESSING,
            Self::Output(_) => EXIT_OUTPUT,
            Self::Cancelled => EXIT_CANCELLED,
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
        Some(Command::Preview(args)) => run_preview(args),
        Some(Command::Filter(args)) => run_filter(args),
        Some(Command::Performance(args)) => run_performance(args),
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

fn run_preview(args: PreviewArgs) -> Result<(), CliError> {
    if !args.volume.is_finite() || !(0.0..=1.0).contains(&args.volume) {
        return Err(CliError::InvalidArgument(
            "volume must be finite and in range 0..=1".to_owned(),
        ));
    }
    let result_dir = absolute_path(&args.result_dir)?;
    let report: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(result_dir.join("report.json")).map_err(
            |error| CliError::InvalidArgument(format!("cannot read result report: {error}")),
        )?)
        .map_err(|error| {
            CliError::InvalidArgument(format!("result report is invalid JSON: {error}"))
        })?;
    let preview_relative = report
        .get("artifacts")
        .and_then(serde_json::Value::as_array)
        .and_then(|artifacts| {
            artifacts.iter().find_map(|artifact| {
                (artifact.get("kind").and_then(serde_json::Value::as_str) == Some("preview_wav"))
                    .then(|| {
                        artifact
                            .get("relative_path")
                            .and_then(serde_json::Value::as_str)
                    })
                    .flatten()
            })
        })
        .ok_or_else(|| {
            CliError::InvalidArgument(
                "result has no preview.wav; rerun with --preview-wav".to_owned(),
            )
        })?;
    let preview_path = crate::jobs::safe_join(&result_dir, preview_relative)?;
    if !preview_path.is_file() {
        return Err(CliError::InvalidArgument(format!(
            "preview artifact is missing: {}",
            preview_path.display()
        )));
    }
    let mut playback = crate::preview::PlaybackService::open_wav(preview_path.clone());
    playback
        .set_volume(args.volume)
        .map_err(|error| CliError::Environment(error.to_string()))?;
    if let Some(error) = playback.error() {
        return Err(CliError::Environment(error.to_string()));
    }
    playback
        .play()
        .map_err(|error| CliError::Environment(error.to_string()))?;
    println!("preview: {}", preview_path.display());
    let cancellation = cancellation_flag()?;
    while !playback.is_finished() {
        if cancellation.load(Ordering::Relaxed) {
            let _ = playback.stop();
            return Err(CliError::Cancelled);
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let _ = playback.stop();
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
    let cleaning = build_cleaning_options(
        args.cleaning_profile,
        args.min_confidence,
        args.min_duration_ms,
        args.retrigger_gap_ms,
    )?;
    let arrangement =
        build_arrangement_options(args.arrangement, args.onset_window_ms, args.max_voices)?;
    run_job(
        args.worker,
        args.input,
        args.output,
        Operation::Transcribe,
        options,
        cleaning,
        arrangement,
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
    let cleaning = build_cleaning_options(
        args.cleaning_profile,
        args.min_confidence,
        args.min_duration_ms,
        args.retrigger_gap_ms,
    )?;
    let arrangement =
        build_arrangement_options(args.arrangement, args.onset_window_ms, args.max_voices)?;
    run_job(
        args.worker,
        args.input,
        args.output,
        Operation::ConvertMidi,
        options,
        cleaning,
        arrangement,
        args.json,
    )
}

fn run_filter(args: FilterArgs) -> Result<(), CliError> {
    let filter = match &args.filter_file {
        Some(path) => {
            let filter_text = fs::read_to_string(path)?;
            let filter: FilterSpec = serde_json::from_str(&filter_text).map_err(|error| {
                CliError::InvalidArgument(format!("invalid filter file: {error}"))
            })?;
            filter.validate().map_err(CliError::InvalidArgument)?;
            Some(filter)
        }
        None => None,
    };
    let filter_preset = if args.auto_filter {
        Some(FilterPreset::Auto)
    } else {
        args.preset.map(Into::into)
    };
    let options = StartOptions {
        filter,
        filter_preset,
        overwrite: Some(args.overwrite),
        ..StartOptions::default()
    };
    run_job(
        args.worker,
        args.result_dir,
        args.output,
        Operation::Refilter,
        options,
        CleaningOptions::default(),
        ArrangementOptions::default(),
        args.json,
    )
}

fn run_performance(args: PerformanceArgs) -> Result<(), CliError> {
    let options = StartOptions {
        title: args.title,
        preview_wav: Some(args.preview_wav),
        overwrite: Some(args.overwrite),
        ..StartOptions::default()
    };
    run_job(
        args.worker,
        args.result_dir,
        args.output,
        Operation::RenderPerformance,
        options,
        CleaningOptions::default(),
        ArrangementOptions::default(),
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
        filter: None,
        filter_preset: None,
        title: None,
    })
}

pub(crate) fn build_cleaning_options(
    profile: CleaningProfile,
    min_confidence: Option<f64>,
    min_duration_ms: Option<u64>,
    retrigger_gap_ms: Option<u64>,
) -> Result<CleaningOptions, CliError> {
    CleaningOptions {
        profile,
        min_confidence,
        min_duration_us: min_duration_ms.map(|value| value * 1_000),
        retrigger_gap_us: retrigger_gap_ms.map(|value| value * 1_000),
    }
    .validate()
}

pub(crate) fn build_arrangement_options(
    profile: ArrangementProfile,
    onset_window_ms: u64,
    max_voices: u8,
) -> Result<ArrangementOptions, CliError> {
    let onset_window_us = onset_window_ms
        .checked_mul(1_000)
        .ok_or_else(|| CliError::InvalidArgument("onset-window-ms is out of range".to_owned()))?;
    ArrangementOptions {
        profile,
        onset_window_us,
        max_voices,
    }
    .validate()
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

#[allow(clippy::too_many_arguments)]
fn run_job(
    worker_override: Option<PathBuf>,
    input: PathBuf,
    output: PathBuf,
    operation: Operation,
    options: StartOptions,
    cleaning: CleaningOptions,
    arrangement: ArrangementOptions,
    json_output: bool,
) -> Result<(), CliError> {
    let cancellation = cancellation_flag()?;
    let outcome = run_job_with_cancel(
        worker_override,
        input,
        output,
        operation,
        options,
        cleaning,
        arrangement,
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

#[allow(clippy::too_many_arguments)]
pub(crate) fn run_job_with_cancel<F>(
    worker_override: Option<PathBuf>,
    input: PathBuf,
    output: PathBuf,
    operation: Operation,
    options: StartOptions,
    cleaning: CleaningOptions,
    arrangement: ArrangementOptions,
    cancellation: Arc<AtomicBool>,
    mut on_update: F,
) -> Result<JobOutcome, CliError>
where
    F: FnMut(JobUpdate),
{
    let input = absolute_path(&input)?;
    let input_valid = match operation {
        Operation::Refilter | Operation::RenderPerformance => input.is_dir(),
        Operation::Transcribe | Operation::ConvertMidi => input.is_file(),
    };
    if !input_valid {
        return Err(CliError::InvalidArgument(format!(
            "input path does not exist: {}",
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
    if matches!(operation, Operation::Transcribe | Operation::ConvertMidi)
        && output.exists()
        && overwrite
        && input.starts_with(&output)
    {
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
    let spec = resolve_worker_spec(worker_override)?
        .with_env(cleaning.worker_env())
        .with_env(arrangement.worker_env());
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

pub(crate) fn resolve_worker_spec(override_path: Option<PathBuf>) -> Result<WorkerSpec, CliError> {
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
