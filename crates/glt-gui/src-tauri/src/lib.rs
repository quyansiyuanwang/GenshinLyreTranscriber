use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use glt::desktop::{self, DesktopJobRequest};
use glt::jobs::{Operation, Timing, Transpose};
use glt::preview::PlaybackService;
use project::{ProjectDocument, ProjectRevision};
use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager, State};
use uuid::Uuid;

mod analysis;
mod project;
mod separation;

#[derive(Default)]
struct GuiState {
    running: AtomicBool,
    cancellation: Mutex<Option<Arc<AtomicBool>>>,
    playback: Mutex<Option<PlaybackService>>,
    project: Mutex<Option<project::OpenProject>>,
    worker_path: Mutex<Option<PathBuf>>,
}

impl GuiState {
    fn lock_cancellation(
        &self,
    ) -> Result<std::sync::MutexGuard<'_, Option<Arc<AtomicBool>>>, String> {
        self.cancellation
            .lock()
            .map_err(|_| "job cancellation state is unavailable".to_owned())
    }

    fn lock_playback(&self) -> Result<std::sync::MutexGuard<'_, Option<PlaybackService>>, String> {
        self.playback
            .lock()
            .map_err(|_| "preview state is unavailable".to_owned())
    }

    fn lock_project(
        &self,
    ) -> Result<std::sync::MutexGuard<'_, Option<project::OpenProject>>, String> {
        self.project
            .lock()
            .map_err(|_| "project state is unavailable".to_owned())
    }

    fn lock_worker_path(&self) -> Result<std::sync::MutexGuard<'_, Option<PathBuf>>, String> {
        self.worker_path
            .lock()
            .map_err(|_| "worker path state is unavailable".to_owned())
    }
}

#[tauri::command]
async fn project_create(
    state: State<'_, GuiState>,
    path: PathBuf,
    name: String,
    source: Option<PathBuf>,
) -> Result<ProjectDocument, String> {
    let opened = tauri::async_runtime::spawn_blocking(move || project::create(&path, name, source))
        .await
        .map_err(|error| format!("project task failed: {error}"))??;
    let mut current = state.lock_project()?;
    if current.is_some() {
        return Err("close the current project before creating another".to_owned());
    }
    let document = opened.document().clone();
    *current = Some(opened);
    Ok(document)
}

#[tauri::command]
fn project_open(state: State<'_, GuiState>, path: PathBuf) -> Result<ProjectDocument, String> {
    let mut current = state.lock_project()?;
    if current.is_some() {
        return Err("close the current project before opening another".to_owned());
    }
    let opened = project::open(&path)?;
    let document = opened.document().clone();
    *current = Some(opened);
    Ok(document)
}

#[tauri::command]
fn project_current(state: State<'_, GuiState>) -> Result<Option<ProjectDocument>, String> {
    Ok(state
        .lock_project()?
        .as_ref()
        .map(|project| project.document().clone()))
}

#[tauri::command]
fn project_save(
    state: State<'_, GuiState>,
    name: Option<String>,
) -> Result<ProjectDocument, String> {
    let mut current = state.lock_project()?;
    let project = current
        .as_mut()
        .ok_or_else(|| "no project is open".to_owned())?;
    project::save(project, name)?;
    Ok(project.document().clone())
}

#[tauri::command]
fn project_relink_source(
    state: State<'_, GuiState>,
    source: PathBuf,
) -> Result<ProjectDocument, String> {
    let mut current = state.lock_project()?;
    let project = current
        .as_mut()
        .ok_or_else(|| "no project is open".to_owned())?;
    project::relink_source(project, source)?;
    Ok(project.document().clone())
}

#[tauri::command]
fn project_add_revision(
    state: State<'_, GuiState>,
    kind: String,
    relative_path: String,
    parent_id: Option<String>,
) -> Result<ProjectRevision, String> {
    let mut current = state.lock_project()?;
    let project = current
        .as_mut()
        .ok_or_else(|| "no project is open".to_owned())?;
    project::add_revision(project, kind, relative_path, parent_id)
}

#[tauri::command]
fn project_add_revision_path(
    state: State<'_, GuiState>,
    kind: String,
    absolute_path: PathBuf,
    parent_id: Option<String>,
) -> Result<ProjectDocument, String> {
    let mut current = state.lock_project()?;
    let project = current
        .as_mut()
        .ok_or_else(|| "no project is open".to_owned())?;
    project::add_revision_absolute(project, kind, absolute_path, parent_id)?;
    Ok(project.document().clone())
}

#[tauri::command]
fn project_close(state: State<'_, GuiState>) -> Result<(), String> {
    *state.lock_project()? = None;
    Ok(())
}

#[tauri::command]
fn doctor(
    state: State<'_, GuiState>,
    worker_path: Option<PathBuf>,
) -> Result<desktop::DesktopDoctorInfo, String> {
    desktop::doctor(worker_path.or(state.lock_worker_path()?.clone()))
}

#[tauri::command]
fn start_job(
    app: AppHandle,
    state: State<'_, GuiState>,
    mut request: DesktopJobRequest,
) -> Result<(), String> {
    if request.worker_path.is_none() {
        request.worker_path = state.lock_worker_path()?.clone();
    }
    spawn_desktop_job(app, &state, request, None)
}

fn spawn_desktop_job(
    app: AppHandle,
    state: &GuiState,
    request: DesktopJobRequest,
    cleanup: Option<PathBuf>,
) -> Result<(), String> {
    if state.running.swap(true, Ordering::SeqCst) {
        return Err("a job is already running".to_owned());
    }
    let cancellation = Arc::new(AtomicBool::new(false));
    *state.lock_cancellation()? = Some(Arc::clone(&cancellation));
    let app_handle = app.clone();
    std::thread::spawn(move || {
        let event_app = app_handle.clone();
        let result = desktop::run_job(request, Arc::clone(&cancellation), move |event| {
            let _ = event_app.emit("job-event", event);
        });

        if result.is_ok() {
            if let Ok(result) = result {
                let _ = app_handle.emit("job-finished", result);
            }
        } else if cancellation.load(Ordering::SeqCst) {
            let _ = app_handle.emit("job-cancelled", ());
        } else if let Err(error) = result {
            let _ = app_handle.emit("job-failed", error);
        }

        let state = app_handle.state::<GuiState>();
        state.running.store(false, Ordering::SeqCst);
        if let Ok(mut active) = state.cancellation.lock() {
            *active = None;
        }
        if let Some(path) = cleanup {
            let _ = fs::remove_file(path);
        }
    });
    Ok(())
}

#[tauri::command]
fn cancel_job(state: State<'_, GuiState>) -> Result<(), String> {
    let active = state.lock_cancellation()?;
    if let Some(cancellation) = active.as_ref() {
        cancellation.store(true, Ordering::SeqCst);
    }
    Ok(())
}

#[tauri::command]
fn read_report(result_dir: PathBuf) -> Result<Value, String> {
    let path = result_dir.join("report.json");
    let text = std::fs::read_to_string(&path)
        .map_err(|error| format!("cannot read {}: {error}", path.display()))?;
    serde_json::from_str(&text).map_err(|error| format!("invalid report JSON: {error}"))
}

#[tauri::command]
fn next_filter_output(source: PathBuf) -> Result<PathBuf, String> {
    let parent = source
        .parent()
        .ok_or_else(|| "result directory has no parent".to_owned())?;
    let name = source
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| "result directory name is not UTF-8".to_owned())?;
    let base = name
        .rfind("-filter-")
        .filter(|index| {
            name[index + 8..]
                .chars()
                .all(|character| character.is_ascii_digit())
        })
        .map_or(name, |index| &name[..index]);
    for index in 1..=999 {
        let candidate = parent.join(format!("{base}-filter-{index:02}"));
        if !candidate.exists() {
            return Ok(candidate);
        }
    }
    Err("no available filter variant name remains".to_owned())
}

#[tauri::command]
fn read_performance(result_dir: PathBuf) -> Result<Value, String> {
    let path = result_dir.join("performance.json");
    let text = fs::read_to_string(&path)
        .map_err(|error| format!("cannot read {}: {error}", path.display()))?;
    serde_json::from_str(&text).map_err(|error| format!("invalid performance JSON: {error}"))
}

#[tauri::command]
fn read_candidate_overlay(result_dir: PathBuf) -> Result<Value, String> {
    let path = result_dir.join("score.candidates.json");
    if !path.is_file() {
        return Ok(Value::Array(Vec::new()));
    }
    let text = fs::read_to_string(&path)
        .map_err(|error| format!("cannot read {}: {error}", path.display()))?;
    let document: Value =
        serde_json::from_str(&text).map_err(|error| format!("invalid candidate JSON: {error}"))?;
    Ok(document
        .pointer("/sequence/notes")
        .cloned()
        .unwrap_or_else(|| Value::Array(Vec::new())))
}

#[tauri::command]
fn read_stem_set(path: PathBuf) -> Result<Value, String> {
    let text = fs::read_to_string(&path)
        .map_err(|error| format!("cannot read {}: {error}", path.display()))?;
    serde_json::from_str(&text).map_err(|error| format!("invalid stem set JSON: {error}"))
}

#[tauri::command]
fn next_edit_output(source: PathBuf) -> Result<PathBuf, String> {
    next_edit_output_path(&source)
}

fn next_edit_output_path(source: &Path) -> Result<PathBuf, String> {
    let parent = source
        .parent()
        .ok_or_else(|| "result directory has no parent".to_owned())?;
    let name = source
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| "result directory name is not UTF-8".to_owned())?;
    let base = name
        .rfind("-edit-")
        .filter(|index| {
            name[index + 6..]
                .chars()
                .all(|character| character.is_ascii_digit())
        })
        .map_or(name, |index| &name[..index]);
    for index in 1..=999 {
        let candidate = parent.join(format!("{base}-edit-{index:02}"));
        if !candidate.exists() {
            return Ok(candidate);
        }
    }
    Err("no available edit revision name remains".to_owned())
}

#[tauri::command]
#[allow(clippy::too_many_arguments)]
fn edit_export(
    app: AppHandle,
    state: State<'_, GuiState>,
    source_result_dir: PathBuf,
    output: PathBuf,
    performance: Value,
    preview_wav: bool,
    title: Option<String>,
    worker_path: Option<PathBuf>,
) -> Result<(), String> {
    if !source_result_dir.is_dir() {
        return Err("source performance result directory does not exist".to_owned());
    }
    if output.exists() {
        return Err(format!("edit output already exists: {}", output.display()));
    }
    let revision_id = output
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty() && value.len() <= 128)
        .ok_or_else(|| "edit output name is invalid".to_owned())?
        .to_owned();
    let parent_revision_id = performance
        .pointer("/revision/id")
        .and_then(Value::as_str)
        .ok_or_else(|| "performance revision id is missing".to_owned())?
        .to_owned();
    let parent = output
        .parent()
        .ok_or_else(|| "edit output must have a parent directory".to_owned())?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let payload = parent.join(format!(".glt-edit-{}.json", Uuid::new_v4()));
    let mut encoded = serde_json::to_vec_pretty(&performance).map_err(|error| error.to_string())?;
    encoded.push(b'\n');
    fs::write(&payload, encoded).map_err(|error| error.to_string())?;
    let request = DesktopJobRequest {
        input: payload.clone(),
        output: output.clone(),
        operation: Operation::EditExport,
        timing: Timing::Preserve,
        bpm: None,
        transpose: Transpose::Semitones(0),
        audio_track: None,
        start_seconds: None,
        end_seconds: None,
        preview_wav,
        overwrite: false,
        cleaning_profile: Default::default(),
        min_confidence: None,
        min_duration_ms: None,
        retrigger_gap_ms: None,
        arrangement: Default::default(),
        onset_window_ms: 150,
        max_voices: 21,
        filter: None,
        filter_preset: None,
        source_result_dir: Some(source_result_dir),
        revision_id: Some(revision_id),
        parent_revision_id: Some(parent_revision_id),
        title,
        worker_path: worker_path.or(state.lock_worker_path()?.clone()),
    };
    if let Err(error) = spawn_desktop_job(app, &state, request, Some(payload.clone())) {
        let _ = fs::remove_file(payload);
        return Err(error);
    }
    Ok(())
}

#[tauri::command]
fn play_preview(state: State<'_, GuiState>, path: PathBuf, volume: f32) -> Result<(), String> {
    let mut playback = state.lock_playback()?;
    let should_reopen = playback
        .as_ref()
        .is_none_or(|service| service.path() != Path::new(&path));
    if should_reopen {
        *playback = Some(PlaybackService::open_wav(path));
    }
    let service = playback
        .as_mut()
        .ok_or_else(|| "preview service is unavailable".to_owned())?;
    service
        .set_volume(volume)
        .map_err(|error| error.to_string())?;
    service.play().map_err(|error| error.to_string())
}

#[tauri::command]
fn play_ab_source(
    state: State<'_, GuiState>,
    path: PathBuf,
    volume: f32,
    position_us: u64,
) -> Result<(), String> {
    let position = std::time::Duration::from_micros(position_us);
    let mut playback = state.lock_playback()?;
    if let Some(service) = playback.as_mut() {
        service
            .set_volume(volume)
            .map_err(|error| error.to_string())?;
        if service.path() == path {
            service
                .seek(position)
                .and_then(|()| service.play())
                .map_err(|error| error.to_string())
        } else {
            service
                .switch_path(path, position)
                .map_err(|error| error.to_string())
        }
    } else {
        let mut service = PlaybackService::open_wav(path);
        service
            .set_volume(volume)
            .and_then(|()| service.seek(position))
            .and_then(|()| service.play())
            .map_err(|error| error.to_string())?;
        *playback = Some(service);
        Ok(())
    }
}

#[tauri::command]
fn pause_preview(state: State<'_, GuiState>) -> Result<(), String> {
    let mut playback = state.lock_playback()?;
    playback
        .as_mut()
        .ok_or_else(|| "no preview is loaded".to_owned())?
        .pause()
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn stop_preview(state: State<'_, GuiState>) -> Result<(), String> {
    let mut playback = state.lock_playback()?;
    if let Some(service) = playback.as_mut() {
        service.stop().map_err(|error| error.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn set_preview_volume(state: State<'_, GuiState>, volume: f32) -> Result<(), String> {
    let mut playback = state.lock_playback()?;
    if let Some(service) = playback.as_mut() {
        service
            .set_volume(volume)
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn playback_status(state: State<'_, GuiState>) -> Result<analysis::PlaybackStatus, String> {
    let playback = state.lock_playback()?;
    Ok(match playback.as_ref() {
        Some(service) => analysis::PlaybackStatus {
            position_us: u64::try_from(service.position().as_micros()).unwrap_or(u64::MAX),
            paused: service.is_paused(),
            available: service.is_available(),
        },
        None => analysis::PlaybackStatus {
            position_us: 0,
            paused: true,
            available: false,
        },
    })
}

#[tauri::command]
fn seek_playback(state: State<'_, GuiState>, position_us: u64) -> Result<(), String> {
    let mut playback = state.lock_playback()?;
    playback
        .as_mut()
        .ok_or_else(|| "no preview is loaded".to_owned())?
        .seek(std::time::Duration::from_micros(position_us))
        .map_err(|error| error.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(GuiState::default())
        .manage(analysis::AnalysisState::default())
        .manage(separation::SeparationState::default())
        .setup(|app| {
            let resource = app.path().resource_dir()?;
            let worker = if cfg!(windows) {
                resource.join("glt-worker").join("glt-worker.exe")
            } else {
                resource.join("glt-worker").join("glt-worker")
            };
            if worker.is_file() {
                let state = app.state::<GuiState>();
                *state.lock_worker_path().map_err(std::io::Error::other)? = Some(worker);
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            separation::separator_component_status,
            separation::separator_component_install,
            separation::separator_component_uninstall,
            separation::start_separation,
            separation::start_routing,
            separation::cancel_separation,
            analysis::start_analysis,
            analysis::cancel_analysis,
            analysis::analysis_manifest,
            analysis::analysis_waveform,
            analysis::analysis_spectrogram_image,
            analysis::analysis_spectrum,
            project_create,
            project_open,
            project_current,
            project_save,
            project_relink_source,
            project_add_revision,
            project_add_revision_path,
            project_close,
            doctor,
            start_job,
            cancel_job,
            read_report,
            next_filter_output,
            read_performance,
            read_candidate_overlay,
            read_stem_set,
            next_edit_output,
            edit_export,
            play_preview,
            play_ab_source,
            pause_preview,
            stop_preview,
            set_preview_volume,
            playback_status,
            seek_playback,
        ])
        .run(tauri::generate_context!())
        .expect("error while running GenshinLyreTranscriber desktop");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn edit_output_names_increment_without_overwriting() {
        let root = std::env::temp_dir().join(format!("glt-edit-name-{}", Uuid::new_v4()));
        fs::create_dir_all(root.join("result-edit-01")).unwrap();
        let source = root.join("result-edit-01");
        let next = next_edit_output_path(&source).unwrap();
        assert_eq!(next, root.join("result-edit-02"));
        fs::create_dir_all(&next).unwrap();
        assert_eq!(
            next_edit_output_path(&source).unwrap(),
            root.join("result-edit-03")
        );
        let _ = fs::remove_dir_all(root);
    }
}
