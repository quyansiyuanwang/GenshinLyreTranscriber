//! Public bridge used by the Tauri desktop application.

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;

use serde::{Deserialize, Serialize};

use crate::cli::{
    ArrangementProfile, CleaningProfile, JobUpdate, build_arrangement_options,
    build_cleaning_options, build_job_options, resolve_worker_spec, run_job_with_cancel,
};
use crate::jobs::{
    DEFAULT_READY_TIMEOUT, FilterPreset, FilterSpec, Operation, ResultPayload, Timing, Transpose,
    WorkerClient, WorkerSpec,
};

#[derive(Debug, Clone, Copy, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DesktopCleaningProfile {
    #[default]
    Auto,
    Solo,
    Mix,
    Strict,
}

impl From<DesktopCleaningProfile> for CleaningProfile {
    fn from(value: DesktopCleaningProfile) -> Self {
        match value {
            DesktopCleaningProfile::Auto => Self::Auto,
            DesktopCleaningProfile::Solo => Self::Solo,
            DesktopCleaningProfile::Mix => Self::Mix,
            DesktopCleaningProfile::Strict => Self::Strict,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DesktopArrangementProfile {
    #[default]
    Balanced,
    Off,
}

impl From<DesktopArrangementProfile> for ArrangementProfile {
    fn from(value: DesktopArrangementProfile) -> Self {
        match value {
            DesktopArrangementProfile::Balanced => Self::Balanced,
            DesktopArrangementProfile::Off => Self::Off,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DesktopJobRequest {
    pub input: PathBuf,
    pub output: PathBuf,
    pub operation: Operation,
    pub timing: Timing,
    pub bpm: Option<f64>,
    pub transpose: Transpose,
    pub audio_track: Option<u32>,
    pub start_seconds: Option<f64>,
    pub end_seconds: Option<f64>,
    pub preview_wav: bool,
    pub overwrite: bool,
    #[serde(default)]
    pub cleaning_profile: DesktopCleaningProfile,
    pub min_confidence: Option<f64>,
    pub min_duration_ms: Option<u64>,
    pub retrigger_gap_ms: Option<u64>,
    #[serde(default)]
    pub arrangement: DesktopArrangementProfile,
    pub onset_window_ms: u64,
    pub max_voices: u8,
    pub filter: Option<FilterSpec>,
    pub filter_preset: Option<FilterPreset>,
    pub worker_path: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum DesktopJobEvent {
    Progress {
        stage: String,
        fraction: Option<f64>,
    },
    Warning {
        code: String,
        message: String,
    },
}

#[derive(Debug, Clone, Serialize)]
pub struct DesktopJobResult {
    pub job_id: String,
    pub result: ResultPayload,
}

#[derive(Debug, Clone, Serialize)]
pub struct DesktopDoctorInfo {
    pub worker_version: String,
    pub application_version: String,
    pub model_version: String,
}

pub fn run_job<F>(
    request: DesktopJobRequest,
    cancellation: Arc<AtomicBool>,
    mut on_event: F,
) -> Result<DesktopJobResult, String>
where
    F: FnMut(DesktopJobEvent),
{
    let mut options = build_job_options(
        request.timing,
        request.bpm,
        request.transpose,
        request.audio_track,
        request.start_seconds,
        request.end_seconds,
        request.preview_wav,
        request.overwrite,
    )
    .map_err(|error| error.to_string())?;
    options.filter = request.filter;
    options.filter_preset = request.filter_preset;

    let cleaning = build_cleaning_options(
        request.cleaning_profile.into(),
        request.min_confidence,
        request.min_duration_ms,
        request.retrigger_gap_ms,
    )
    .map_err(|error| error.to_string())?;
    let arrangement = build_arrangement_options(
        request.arrangement.into(),
        request.onset_window_ms,
        request.max_voices,
    )
    .map_err(|error| error.to_string())?;

    let outcome = run_job_with_cancel(
        request.worker_path,
        request.input,
        request.output,
        request.operation,
        options,
        cleaning,
        arrangement,
        cancellation,
        |update| on_event(map_update(update)),
    )
    .map_err(|error| error.to_string())?;

    Ok(DesktopJobResult {
        job_id: outcome.job_id,
        result: outcome.result,
    })
}

pub fn doctor(worker_path: Option<PathBuf>) -> Result<DesktopDoctorInfo, String> {
    let spec = resolve_worker_spec(worker_path).map_err(|error| error.to_string())?;
    let client =
        WorkerClient::launch(spec, DEFAULT_READY_TIMEOUT).map_err(|error| error.to_string())?;
    let ready = client.ready();
    Ok(DesktopDoctorInfo {
        worker_version: ready.worker_version.clone(),
        application_version: ready.application_version.clone(),
        model_version: ready.model_version.clone(),
    })
}

pub fn worker_spec(worker_path: Option<PathBuf>) -> Result<WorkerSpec, String> {
    resolve_worker_spec(worker_path).map_err(|error| error.to_string())
}

fn map_update(update: JobUpdate) -> DesktopJobEvent {
    match update {
        JobUpdate::Progress { stage, fraction } => DesktopJobEvent::Progress { stage, fraction },
        JobUpdate::Warning { code, message } => DesktopJobEvent::Warning { code, message },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn desktop_profiles_use_stable_snake_case_names() {
        let cleaning: DesktopCleaningProfile = serde_json::from_str("\"strict\"").unwrap();
        assert!(matches!(cleaning, DesktopCleaningProfile::Strict));
        let arrangement: DesktopArrangementProfile = serde_json::from_str("\"balanced\"").unwrap();
        assert!(matches!(arrangement, DesktopArrangementProfile::Balanced));
    }
}
