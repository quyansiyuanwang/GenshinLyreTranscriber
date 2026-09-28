use crate::process_tree::ProcessTree;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use uuid::Uuid;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager, State};
use zip::ZipArchive;

const MANIFEST_NAME: &str = "separator-component-v1.json";

#[derive(Default, Clone)]
pub struct SeparationState {
    running: Arc<AtomicBool>,
    cancelled: Arc<AtomicBool>,
    child: Arc<Mutex<Option<ProcessTree>>>,
}

impl SeparationState {
    fn lock_child(&self) -> Result<std::sync::MutexGuard<'_, Option<ProcessTree>>, String> {
        self.child
            .lock()
            .map_err(|_| "separation process state is unavailable".to_owned())
    }
}

// The guard is shared by jobs and component mutations, and releases on every error path.
struct OperationGuard(Arc<AtomicBool>);
impl OperationGuard {
    fn acquire(state: &SeparationState) -> Result<Self, String> {
        if state
            .running
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return Err("分离、路由或组件维护正在进行，请等待完成后重试".to_owned());
        }
        Ok(Self(Arc::clone(&state.running)))
    }
}
impl Drop for OperationGuard {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

fn drain_diagnostics(mut stream: impl Read) -> String {
    const LIMIT: usize = 16 * 1024;
    let mut tail = std::collections::VecDeque::with_capacity(LIMIT);
    let mut buffer = [0_u8; 4096];
    loop {
        match stream.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(count) => {
                for byte in &buffer[..count] {
                    if tail.len() == LIMIT {
                        tail.pop_front();
                    }
                    tail.push_back(*byte);
                }
            }
        }
    }
    String::from_utf8_lossy(&tail.into_iter().collect::<Vec<_>>()).into_owned()
}

#[derive(Debug, Clone, Serialize)]
struct CancellationEvent {
    format_version: u8,
    operation: &'static str,
    state: &'static str,
}

impl CancellationEvent {
    fn new(operation: &'static str) -> Self {
        Self {
            format_version: 1,
            operation,
            state: "cancelled",
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SeparationRequest {
    pub component: PathBuf,
    pub input: PathBuf,
    pub output: PathBuf,
    pub model: String,
    pub worker_path: Option<PathBuf>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RoutingRequest {
    pub stem_set: PathBuf,
    pub output: PathBuf,
    pub mode: String,
    pub max_voices: Option<u8>,
    pub plan: Option<Value>,
    pub worker_path: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SeparatorModelStatus {
    pub id: String,
    pub quality: String,
    pub logical_sha256: String,
    pub verified: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct SeparatorComponentStatus {
    pub installed: bool,
    pub directory: PathBuf,
    pub component_id: Option<String>,
    pub component_version: Option<String>,
    pub models: Vec<SeparatorModelStatus>,
    pub error: Option<String>,
}

#[tauri::command]
pub async fn start_separation(app: AppHandle, request: SeparationRequest) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let spec = glt::desktop::worker_spec(request.worker_path)?;
        let mut command = Command::new(&spec.program);
        command.args(&spec.args).envs(spec.env);
        if let Some(directory) = spec.working_directory {
            command.current_dir(directory);
        }
        command
            .arg("separate")
            .arg("--component")
            .arg(&request.component)
            .arg("--input")
            .arg(&request.input)
            .arg("--output")
            .arg(&request.output)
            .arg("--model")
            .arg(&request.model);
        let state = app.state::<SeparationState>();
        spawn_json_process(app.clone(), &state, command, "separation")
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn start_routing(app: AppHandle, request: RoutingRequest) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let spec = glt::desktop::worker_spec(request.worker_path)?;
        let mut command = Command::new(&spec.program);
        command.args(&spec.args).envs(spec.env);
        if let Some(directory) = spec.working_directory {
            command.current_dir(directory);
        }
        command
            .arg("route")
            .arg("--stem-set")
            .arg(&request.stem_set)
            .arg("--output")
            .arg(&request.output);
        if let Some(plan) = request.plan {
            let encoded = serde_json::to_string(&plan).map_err(|error| error.to_string())?;
            command.arg("--plan-json").arg(encoded);
        } else {
            command.arg("--mode").arg(&request.mode);
            if let Some(max_voices) = request.max_voices {
                command.arg("--max-voices").arg(max_voices.to_string());
            }
        }
        let state = app.state::<SeparationState>();
        spawn_json_process(app.clone(), &state, command, "routing")
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn cancel_separation(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<SeparationState>();
        cancel_process(&state)
    })
    .await
    .map_err(|error| error.to_string())?
}

pub(crate) fn cancel_process(state: &SeparationState) -> Result<(), String> {
    let mut slot = state.lock_child()?;
    if let Some(process) = slot.as_mut() {
        process.terminate().map_err(|error| error.to_string())?;
        state.cancelled.store(true, Ordering::SeqCst);
    }
    Ok(())
}

pub(crate) fn spawn_json_process(
    app: AppHandle,
    state: &SeparationState,
    mut command: Command,
    prefix: &'static str,
) -> Result<(), String> {
    let guard = OperationGuard::acquire(state)?;
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut slot = state.lock_child()?;
    state.cancelled.store(false, Ordering::SeqCst);
    let mut process = ProcessTree::spawn(&mut command)
        .map_err(|error| format!("cannot start {prefix} worker: {error}"))?;
    let stdout = process
        .child
        .stdout
        .take()
        .ok_or("worker stdout is unavailable")?;
    let stderr = process
        .child
        .stderr
        .take()
        .ok_or("worker stderr is unavailable")?;
    *slot = Some(process);
    drop(slot);
    let status = state.clone();
    std::thread::spawn(move || {
        let stderr_reader = std::thread::spawn(move || drain_diagnostics(stderr));
        let mut reader = BufReader::new(stdout);
        let mut terminal: Option<Result<Value, String>> = None;
        loop {
            let mut line = Vec::new();
            // A malformed worker must not force an unbounded line allocation.
            match reader.by_ref().take(1_048_577).read_until(b'\n', &mut line) {
                Ok(0) => break,
                Err(error) => {
                    terminal = Some(Err(error.to_string()));
                    break;
                }
                Ok(_) if line.len() > 1_048_576 => {
                    terminal = Some(Err("worker protocol line exceeds 1 MiB".to_owned()));
                    break;
                }
                _ => {}
            }
            let Ok(payload) = serde_json::from_slice::<Value>(&line) else {
                continue;
            };
            match payload.get("type").and_then(Value::as_str) {
                Some("progress") => {
                    let _ = app.emit(&format!("{prefix}-progress"), payload);
                }
                Some("result") => {
                    terminal = Some(Ok(payload));
                    break;
                }
                Some("error") => {
                    terminal = Some(Err(payload
                        .get("message")
                        .and_then(Value::as_str)
                        .unwrap_or("worker failed")
                        .to_owned()));
                    break;
                }
                _ => {}
            }
        }
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut exit_ok = false;
        loop {
            let finished = match status.lock_child() {
                Ok(mut slot) => match slot.as_mut() {
                    Some(process) => match process.child.try_wait() {
                        Ok(Some(exit)) => {
                            exit_ok = exit.success();
                            true
                        }
                        Ok(None) => false,
                        Err(_) => true,
                    },
                    None => true,
                },
                Err(_) => true,
            };
            if finished || status.cancelled.load(Ordering::SeqCst) || Instant::now() >= deadline {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let cleanup = status.lock_child().and_then(|mut slot| {
            if let Some(mut process) = slot.take() {
                process.finish().map_err(|error| error.to_string())
            } else {
                Ok(())
            }
        });
        let details = stderr_reader
            .join()
            .unwrap_or_else(|_| "stderr reader failed".to_owned());
        let cancelled = status.cancelled.load(Ordering::SeqCst);
        // Terminal notifications are emitted only after cleanup and lock release.
        drop(guard);
        if let Err(error) = cleanup {
            let _ = app.emit(
                &format!("{prefix}-failed"),
                format!("worker cleanup failed: {error}"),
            );
        } else if cancelled {
            let _ = app.emit(
                &format!("{prefix}-cancelled"),
                CancellationEvent::new(prefix),
            );
        } else {
            match terminal {
                Some(Ok(payload)) if exit_ok => {
                    let _ = app.emit(&format!("{prefix}-finished"), payload);
                }
                Some(Err(error)) => {
                    let _ = app.emit(&format!("{prefix}-failed"), error);
                }
                _ => {
                    let details = if details.trim().is_empty() {
                        "worker exited without a successful terminal result".to_owned()
                    } else {
                        details
                    };
                    let _ = app.emit(&format!("{prefix}-failed"), details);
                }
            }
        }
    });
    Ok(())
}

// Portable components avoid per-process AppData redirection on packaged Windows hosts.
#[tauri::command]
pub fn separator_component_directory(app: AppHandle) -> Result<PathBuf, String> {
    let executable = std::env::current_exe().map_err(|error| error.to_string())?;
    let app_data = app
        .path()
        .app_data_dir()
        .map_err(|error| error.to_string())?;
    Ok(component_directory_for(&executable, &app_data))
}

fn component_directory_for(executable: &Path, app_data: &Path) -> PathBuf {
    if let Some(parent) = executable.parent() {
        let portable = parent.join("separator");
        if portable.join(MANIFEST_NAME).is_file() {
            return portable;
        }
    }
    app_data.join("separator")
}

#[tauri::command]
pub async fn separator_component_status(
    directory: PathBuf,
) -> Result<SeparatorComponentStatus, String> {
    tauri::async_runtime::spawn_blocking(move || inspect_component_status(directory))
        .await
        .map_err(|error| error.to_string())
}

fn inspect_component_status(directory: PathBuf) -> SeparatorComponentStatus {
    match inspect_component(&directory) {
        Ok((manifest, models)) => SeparatorComponentStatus {
            installed: true,
            directory,
            component_id: manifest
                .get("component_id")
                .and_then(Value::as_str)
                .map(str::to_owned),
            component_version: manifest
                .get("component_version")
                .and_then(Value::as_str)
                .map(str::to_owned),
            models,
            error: None,
        },
        Err(error) => SeparatorComponentStatus {
            installed: false,
            directory,
            component_id: None,
            component_version: None,
            models: Vec::new(),
            error: Some(error),
        },
    }
}

#[tauri::command]
pub async fn separator_component_install(
    state: State<'_, SeparationState>,
    archive: PathBuf,
    target: PathBuf,
    overwrite: bool,
) -> Result<SeparatorComponentStatus, String> {
    let guard = OperationGuard::acquire(&state)?;
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = guard;
        install_separator_component(archive, target, overwrite)
    })
    .await
    .map_err(|error| format!("separator component install task failed: {error}"))?
}

fn install_separator_component(
    archive: PathBuf,
    target: PathBuf,
    overwrite: bool,
) -> Result<SeparatorComponentStatus, String> {
    if !archive.is_file() {
        return Err(format!(
            "separator component archive is missing: {}",
            archive.display()
        ));
    }
    let parent = target
        .parent()
        .ok_or_else(|| "separator component target has no parent".to_owned())?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let staging = parent.join(format!(".separator-{}-partial", Uuid::new_v4()));
    fs::create_dir(&staging).map_err(|error| error.to_string())?;
    let result: Result<(), String> = (|| {
        extract_archive(&archive, &staging)?;
        inspect_component(&staging)?;
        publish_component(&staging, &target, overwrite, move_directory)?;
        Ok(())
    })();
    if staging.exists() {
        let _ = fs::remove_dir_all(&staging);
    }
    result?;
    Ok(inspect_component_status(target))
}

fn publish_component(
    staging: &Path,
    target: &Path,
    overwrite: bool,
    mover: impl Fn(&Path, &Path) -> Result<(), String>,
) -> Result<(), String> {
    let backup = target.with_file_name(format!(".separator-{}-backup", Uuid::new_v4()));
    let had_previous = target.exists();
    if had_previous {
        if !overwrite {
            return Err(format!(
                "separator component already exists: {}",
                target.display()
            ));
        }
        // Do not replace an unrelated user directory.
        if !target.join(MANIFEST_NAME).is_file() {
            return Err("target is not a separator component directory".to_owned());
        }
        mover(target, &backup).map_err(|error| {
            format!(
                "{error}; previous component backup location: {}",
                backup.display()
            )
        })?;
    }
    let publish = mover(staging, target).and_then(|()| inspect_component(target).map(|_| ()));
    if let Err(error) = publish {
        if target.exists()
            && let Err(cleanup) = fs::remove_dir_all(target)
        {
            return Err(format!(
                "{error}; cannot remove failed publication: {cleanup}; previous component retained at {}",
                backup.display()
            ));
        }
        if had_previous && let Err(restore) = mover(&backup, target) {
            return Err(format!(
                "{error}; rollback failed: {restore}; previous component retained at {}",
                backup.display()
            ));
        }
        return Err(error);
    }
    // Cleanup is best effort: preserving a backup is safer than reporting a usable install as failed.
    if had_previous {
        let _ = fs::remove_dir_all(&backup);
    }
    Ok(())
}

fn move_directory(source: &Path, target: &Path) -> Result<(), String> {
    if target.exists() {
        return Err(format!(
            "move destination already exists: {}",
            target.display()
        ));
    }
    if fs::rename(source, target).is_ok() {
        return Ok(());
    }
    if let Err(error) = copy_directory(source, target) {
        let _ = fs::remove_dir_all(target);
        return Err(error);
    }
    // Copy completed before deleting the source, including cross-volume/EFS fallback.
    fs::remove_dir_all(source).map_err(|error| error.to_string())
}

fn copy_directory(source: &Path, target: &Path) -> Result<(), String> {
    fs::create_dir_all(target).map_err(|error| error.to_string())?;
    for entry in fs::read_dir(source).map_err(|error| error.to_string())? {
        let entry = entry.map_err(|error| error.to_string())?;
        let destination = target.join(entry.file_name());
        let file_type = entry.file_type().map_err(|error| error.to_string())?;
        if file_type.is_dir() {
            copy_directory(&entry.path(), &destination)?;
        } else if file_type.is_file() {
            fs::copy(entry.path(), destination).map_err(|error| error.to_string())?;
        } else {
            return Err(format!(
                "separator component contains an unsupported file: {}",
                entry.path().display()
            ));
        }
    }
    Ok(())
}

#[tauri::command]
pub async fn separator_component_uninstall(
    state: State<'_, SeparationState>,
    target: PathBuf,
) -> Result<(), String> {
    let guard = OperationGuard::acquire(&state)?;
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = guard;
        uninstall_separator_component(target)
    })
    .await
    .map_err(|error| error.to_string())?
}

fn uninstall_separator_component(target: PathBuf) -> Result<(), String> {
    if !target.join(MANIFEST_NAME).is_file() {
        return Err(format!(
            "not a separator component directory: {}",
            target.display()
        ));
    }
    fs::remove_dir_all(target).map_err(|error| error.to_string())
}

fn inspect_component(root: &Path) -> Result<(Value, Vec<SeparatorModelStatus>), String> {
    let manifest_path = root.join(MANIFEST_NAME);
    let text = fs::read_to_string(&manifest_path)
        .map_err(|error| format!("separator component is not installed: {error}"))?;
    let manifest: Value = serde_json::from_str(&text)
        .map_err(|error| format!("invalid separator component manifest: {error}"))?;
    if manifest.get("format_version").and_then(Value::as_u64) != Some(1) {
        return Err("unsupported separator component format".to_owned());
    }
    let runtime = manifest
        .get("runtime")
        .and_then(Value::as_object)
        .ok_or_else(|| "separator runtime is missing".to_owned())?;
    verify_file(
        root,
        runtime
            .get("executable")
            .and_then(Value::as_str)
            .ok_or_else(|| "separator executable is missing".to_owned())?,
        runtime.get("sha256").and_then(Value::as_str),
        None,
    )?;
    let license = manifest
        .get("license")
        .and_then(Value::as_object)
        .and_then(|value| value.get("relative_path"))
        .and_then(Value::as_str)
        .ok_or_else(|| "separator license is missing".to_owned())?;
    verify_file(root, license, None, None)?;
    let models = manifest
        .get("models")
        .and_then(Value::as_array)
        .ok_or_else(|| "separator models are missing".to_owned())?;
    let mut statuses = Vec::new();
    for model in models {
        let id = model
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| "separator model id is missing".to_owned())?;
        let quality = model
            .get("quality")
            .and_then(Value::as_str)
            .ok_or_else(|| "separator model quality is missing".to_owned())?;
        let logical_sha256 = model
            .get("logical_sha256")
            .and_then(Value::as_str)
            .ok_or_else(|| "separator logical model hash is missing".to_owned())?;
        let relative = model
            .get("relative_path")
            .and_then(Value::as_str)
            .ok_or_else(|| "separator model path is missing".to_owned())?;
        verify_file(
            root,
            relative,
            model.get("sha256").and_then(Value::as_str),
            model.get("size_bytes").and_then(Value::as_u64),
        )?;
        if relative.ends_with(".bundle.json") {
            verify_model_bundle(root, relative)?;
        }
        statuses.push(SeparatorModelStatus {
            id: id.to_owned(),
            quality: quality.to_owned(),
            logical_sha256: logical_sha256.to_owned(),
            verified: true,
        });
    }
    Ok((manifest, statuses))
}

fn verify_model_bundle(root: &Path, relative: &str) -> Result<(), String> {
    let bundle_path = root.join(relative);
    let bundle: Value =
        serde_json::from_slice(&fs::read(&bundle_path).map_err(|error| error.to_string())?)
            .map_err(|error| format!("invalid model bundle: {error}"))?;
    if bundle.get("format_version").and_then(Value::as_u64) != Some(1) {
        return Err("unsupported model bundle version".to_owned());
    }
    let files = bundle
        .get("files")
        .and_then(Value::as_array)
        .filter(|files| !files.is_empty())
        .ok_or("model bundle has no weight files")?;
    let model_root = bundle_path.parent().ok_or("model bundle has no parent")?;
    for file in files {
        let path = file
            .get("relative_path")
            .and_then(Value::as_str)
            .ok_or("model weight path is missing")?;
        let hash = file
            .get("sha256")
            .and_then(Value::as_str)
            .ok_or("model weight hash is missing")?;
        let size = file
            .get("size_bytes")
            .and_then(Value::as_u64)
            .ok_or("model weight size is missing")?;
        verify_file(model_root, path, Some(hash), Some(size))?;
    }
    Ok(())
}

fn verify_file(
    root: &Path,
    relative: &str,
    expected_hash: Option<&str>,
    expected_size: Option<u64>,
) -> Result<(), String> {
    if Path::new(relative).components().any(|component| {
        !matches!(
            component,
            std::path::Component::Normal(_) | std::path::Component::CurDir
        )
    }) {
        return Err(format!(
            "separator path must stay inside the component: {relative}"
        ));
    }
    let path = root.join(relative);
    if !path.is_file() {
        return Err(format!(
            "separator file is missing: {relative}; resolved path: {}. Reinstall the complete separator component ZIP; copying the executable alone is not sufficient.",
            path.display()
        ));
    }
    let canonical_root = root.canonicalize().map_err(|error| error.to_string())?;
    let canonical_path = path.canonicalize().map_err(|error| error.to_string())?;
    if !canonical_path.starts_with(&canonical_root) {
        return Err(format!(
            "separator path escapes component: {}; use a non-redirected portable component directory",
            canonical_path.display()
        ));
    }
    if let Some(expected) = expected_size
        && path.metadata().map_err(|error| error.to_string())?.len() != expected
    {
        return Err(format!("separator file size mismatch: {relative}"));
    }
    if let Some(expected) = expected_hash
        && sha256_file(&path)? != expected.to_ascii_lowercase()
    {
        return Err(format!("separator file hash mismatch: {relative}"));
    }
    Ok(())
}

fn extract_archive(archive_path: &Path, destination: &Path) -> Result<(), String> {
    let file = File::open(archive_path).map_err(|error| error.to_string())?;
    let mut archive = ZipArchive::new(file).map_err(|error| error.to_string())?;
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(|error| error.to_string())?;
        let enclosed = entry
            .enclosed_name()
            .ok_or_else(|| "separator archive contains an unsafe path".to_owned())?;
        let destination_path = destination.join(enclosed);
        if entry.is_dir() {
            fs::create_dir_all(&destination_path).map_err(|error| error.to_string())?;
            continue;
        }
        if let Some(parent) = destination_path.parent() {
            fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        let mut output = File::create(&destination_path).map_err(|error| error.to_string())?;
        std::io::copy(&mut entry, &mut output).map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|error| error.to_string())?;
    let mut hasher = Sha256::new();
    // Tauri command threads can have small stacks. Keep large hash buffers on the heap.
    let mut buffer = vec![0_u8; 64 * 1024];
    loop {
        let count = file.read(&mut buffer).map_err(|error| error.to_string())?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use uuid::Uuid;
    use zip::ZipWriter;
    use zip::write::SimpleFileOptions;

    #[test]
    fn cancellation_event_matches_shared_fixture() {
        let expected: Value = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/desktop-cancellation-v1.json"
        ))
        .unwrap();
        assert_eq!(
            serde_json::to_value(CancellationEvent::new("separation")).unwrap(),
            expected
        );
    }

    #[test]
    fn operation_guard_excludes_all_mutations_and_releases_on_error() {
        let state = SeparationState::default();
        let guard = OperationGuard::acquire(&state).unwrap();
        assert!(OperationGuard::acquire(&state).is_err());
        drop(guard);
        assert!(OperationGuard::acquire(&state).is_ok());
    }

    #[cfg(windows)]
    #[test]
    fn verbose_worker_does_not_block_stdout_or_cancellation() {
        let mut command = Command::new("powershell.exe");
        command.args(["-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            "[Console]::Error.Write(('x' * 2000000)); [Console]::Out.WriteLine('ready'); Start-Sleep -Seconds 120"]);
        command.stdout(Stdio::piped()).stderr(Stdio::piped());
        let mut tree = ProcessTree::spawn(&mut command).unwrap();
        let stderr = tree.child.stderr.take().unwrap();
        let drainer = std::thread::spawn(move || drain_diagnostics(stderr));
        let mut ready = String::new();
        BufReader::new(tree.child.stdout.take().unwrap())
            .read_line(&mut ready)
            .unwrap();
        assert_eq!(ready.trim(), "ready");
        tree.finish().unwrap();
        assert_eq!(drainer.join().unwrap().len(), 16 * 1024);
    }

    #[test]
    fn diagnostics_are_drained_but_only_bounded_tail_is_retained() {
        let mut input = vec![b'x'; 2 * 1024 * 1024];
        input.extend_from_slice(b"FINAL ERROR");
        let tail = drain_diagnostics(std::io::Cursor::new(input));
        assert_eq!(tail.len(), 16 * 1024);
        assert!(tail.ends_with("FINAL ERROR"));
    }

    #[test]
    fn failed_publication_restores_previous_component() {
        let root = std::env::temp_dir().join(format!("glt-component-rollback-{}", Uuid::new_v4()));
        let target = root.join("installed");
        let staging = root.join("staging");
        fs::create_dir_all(&target).unwrap();
        fs::create_dir_all(&staging).unwrap();
        fs::write(target.join(MANIFEST_NAME), b"old manifest").unwrap();
        fs::write(target.join("user-marker"), b"old runtime").unwrap();
        let result = publish_component(&staging, &target, true, |from, to| {
            if from == staging {
                return Err("simulated publication failure".to_owned());
            }
            move_directory(from, to)
        });
        assert!(
            result
                .unwrap_err()
                .contains("simulated publication failure")
        );
        assert_eq!(
            fs::read(target.join("user-marker")).unwrap(),
            b"old runtime"
        );
        assert!(staging.exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rollback_failure_preserves_backup_and_reports_its_location() {
        let root = std::env::temp_dir().join(format!("glt-component-backup-{}", Uuid::new_v4()));
        let target = root.join("installed");
        let staging = root.join("staging");
        fs::create_dir_all(&target).unwrap();
        fs::create_dir_all(&staging).unwrap();
        fs::write(target.join(MANIFEST_NAME), b"old manifest").unwrap();
        let result = publish_component(&staging, &target, true, |from, to| {
            if from != target {
                return Err("simulated locked target".to_owned());
            }
            move_directory(from, to)
        });
        let error = result.unwrap_err();
        let backup = fs::read_dir(&root)
            .unwrap()
            .flatten()
            .map(|entry| entry.path())
            .find(|path| {
                path.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .ends_with("-backup")
            })
            .unwrap();
        assert!(error.contains(&backup.display().to_string()));
        assert_eq!(
            fs::read(backup.join(MANIFEST_NAME)).unwrap(),
            b"old manifest"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn portable_component_is_preferred_only_when_manifest_exists() {
        let root = std::env::temp_dir().join(format!("glt-portable-component-{}", Uuid::new_v4()));
        let executable = root.join("app/glt-gui.exe");
        let app_data = root.join("app-data");
        assert_eq!(
            component_directory_for(&executable, &app_data),
            app_data.join("separator")
        );
        let portable = root.join("app/separator");
        fs::create_dir_all(&portable).unwrap();
        fs::write(portable.join(MANIFEST_NAME), b"{}").unwrap();
        assert_eq!(component_directory_for(&executable, &app_data), portable);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn desktop_requests_match_shared_frontend_fixture() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/desktop-separation-v1.json"
        ))
        .unwrap();
        let separation: SeparationRequest =
            serde_json::from_value(fixture["separation"].clone()).unwrap();
        assert_eq!(separation.model, "htdemucs");
        assert!(separation.worker_path.is_none());
        let routing: RoutingRequest = serde_json::from_value(fixture["routing"].clone()).unwrap();
        assert_eq!(routing.max_voices, Some(3));
        assert_eq!(routing.stem_set, PathBuf::from("stems/stem-set.json"));
        let mut invalid = fixture["separation"].clone();
        invalid["workerPath"] = Value::Null;
        assert!(serde_json::from_value::<SeparationRequest>(invalid).is_err());
    }

    #[test]
    fn missing_runtime_reports_resolved_path_and_repair_action() {
        let root = std::env::temp_dir().join(format!("glt-missing-component-{}", Uuid::new_v4()));
        let error = verify_file(&root, "worker/glt-separator-worker.exe", None, None).unwrap_err();
        assert!(error.contains(&root.display().to_string()));
        assert!(error.contains("complete separator component ZIP"));
    }

    #[test]
    fn component_install_status_and_uninstall_round_trip() {
        let root = std::env::temp_dir().join(format!("glt-separator-component-{}", Uuid::new_v4()));
        let source = root.join("source");
        fs::create_dir_all(source.join("worker/models")).unwrap();
        fs::create_dir_all(source.join("licenses")).unwrap();
        fs::write(source.join("worker/worker.exe"), b"runtime").unwrap();
        fs::write(source.join("licenses/THIRD_PARTY.txt"), b"MIT").unwrap();
        fs::write(source.join("worker/models/weights.bin"), b"weights").unwrap();
        let bundle = serde_json::json!({"format_version": 1, "files": [{
            "relative_path": "weights.bin", "size_bytes": 7,
            "sha256": sha256_file(&source.join("worker/models/weights.bin")).unwrap()
        }]});
        let bundle_bytes = serde_json::to_vec(&bundle).unwrap();
        fs::write(
            source.join("worker/models/htdemucs.bundle.json"),
            &bundle_bytes,
        )
        .unwrap();
        let manifest = serde_json::json!({
            "format_version": 1,
            "component_id": "demucs-cpu",
            "component_version": "4.1.0",
            "protocol_version": 1,
            "runtime": {
                "executable": "worker/worker.exe",
                "sha256": sha256_file(&source.join("worker/worker.exe")).unwrap(),
                "arguments": []
            },
            "license": {"relative_path": "licenses/THIRD_PARTY.txt"},
            "models": [{
                "id": "htdemucs",
                "quality": "balanced",
                "relative_path": "worker/models/htdemucs.bundle.json",
                "sha256": sha256_file(&source.join("worker/models/htdemucs.bundle.json")).unwrap(),
                "logical_sha256": "a".repeat(64),
                "size_bytes": bundle_bytes.len(),
                "source_url": "https://example.invalid/model",
                "license_name": "MIT"
            }]
        });
        fs::write(
            source.join(MANIFEST_NAME),
            serde_json::to_vec_pretty(&manifest).unwrap(),
        )
        .unwrap();
        inspect_component(&source).unwrap();

        let archive = root.join("component.zip");
        let file = File::create(&archive).unwrap();
        let mut writer = ZipWriter::new(file);
        let options = SimpleFileOptions::default();
        for relative in [
            MANIFEST_NAME,
            "worker/worker.exe",
            "licenses/THIRD_PARTY.txt",
            "worker/models/htdemucs.bundle.json",
            "worker/models/weights.bin",
        ] {
            writer.start_file(relative, options).unwrap();
            writer
                .write_all(&fs::read(source.join(relative)).unwrap())
                .unwrap();
        }
        writer.finish().unwrap();

        let target = root.join("installed");
        let installed = install_separator_component(archive, target.clone(), false).unwrap();
        assert!(installed.installed);
        assert_eq!(installed.models.len(), 1);
        let bad_archive = root.join("broken.zip");
        fs::write(&bad_archive, b"not a zip").unwrap();
        assert!(install_separator_component(bad_archive, target.clone(), true).is_err());
        assert!(inspect_component_status(target.clone()).installed);
        let invalid_archive = root.join("missing-runtime.zip");
        let mut writer = ZipWriter::new(File::create(&invalid_archive).unwrap());
        writer
            .start_file(MANIFEST_NAME, SimpleFileOptions::default())
            .unwrap();
        writer
            .write_all(&fs::read(source.join(MANIFEST_NAME)).unwrap())
            .unwrap();
        writer.finish().unwrap();
        assert!(install_separator_component(invalid_archive, target.clone(), true).is_err());
        assert!(inspect_component_status(target.clone()).installed);
        fs::write(target.join("worker/models/weights.bin"), b"corrupt").unwrap();
        let corrupted = inspect_component_status(target.clone());
        assert!(!corrupted.installed);
        assert!(corrupted.error.unwrap().contains("hash mismatch"));
        uninstall_separator_component(target.clone()).unwrap();
        assert!(!target.exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn hashes_large_runtime_without_large_stack_frame() {
        let source =
            std::env::temp_dir().join(format!("glt-separator-hash-{}.bin", Uuid::new_v4()));
        let payload = vec![0x33_u8; 2 * 1024 * 1024];
        fs::write(&source, &payload).unwrap();
        let mut hasher = Sha256::new();
        hasher.update(&payload);
        let expected = format!("{:x}", hasher.finalize());
        assert_eq!(sha256_file(&source).unwrap(), expected);
        let _ = fs::remove_file(source);
    }
}
