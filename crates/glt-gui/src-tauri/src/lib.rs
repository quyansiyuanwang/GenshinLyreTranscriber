use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use glt::desktop::{self, DesktopJobRequest};
use glt::preview::PlaybackService;
use project::{ProjectDocument, ProjectRevision};
use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager, State};

mod project;

#[derive(Default)]
struct GuiState {
    running: AtomicBool,
    cancellation: Mutex<Option<Arc<AtomicBool>>>,
    playback: Mutex<Option<PlaybackService>>,
    project: Mutex<Option<project::OpenProject>>,
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
fn project_close(state: State<'_, GuiState>) -> Result<(), String> {
    *state.lock_project()? = None;
    Ok(())
}

#[tauri::command]
fn doctor(worker_path: Option<PathBuf>) -> Result<desktop::DesktopDoctorInfo, String> {
    desktop::doctor(worker_path)
}

#[tauri::command]
fn start_job(
    app: AppHandle,
    state: State<'_, GuiState>,
    request: DesktopJobRequest,
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(GuiState::default())
        .invoke_handler(tauri::generate_handler![
            project_create,
            project_open,
            project_current,
            project_save,
            project_relink_source,
            project_add_revision,
            project_close,
            doctor,
            start_job,
            cancel_job,
            read_report,
            next_filter_output,
            play_preview,
            pause_preview,
            stop_preview,
            set_preview_volume,
        ])
        .run(tauri::generate_context!())
        .expect("error while running GenshinLyreTranscriber desktop");
}
