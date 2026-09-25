use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager, State};
use zip::ZipArchive;

const MANIFEST_NAME: &str = "separator-component-v1.json";

#[derive(Default)]
pub struct SeparationState {
    running: AtomicBool,
    child: Mutex<Option<Child>>,
}

impl SeparationState {
    fn lock_child(&self) -> Result<std::sync::MutexGuard<'_, Option<Child>>, String> {
        self.child
            .lock()
            .map_err(|_| "separation process state is unavailable".to_owned())
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
pub fn start_separation(
    app: AppHandle,
    state: State<'_, SeparationState>,
    request: SeparationRequest,
) -> Result<(), String> {
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
    spawn_json_process(app, &state, command, "separation")
}

#[tauri::command]
pub fn start_routing(
    app: AppHandle,
    state: State<'_, SeparationState>,
    request: RoutingRequest,
) -> Result<(), String> {
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
    spawn_json_process(app, &state, command, "routing")
}

#[tauri::command]
pub fn cancel_separation(state: State<'_, SeparationState>) -> Result<(), String> {
    if let Some(child) = state.lock_child()?.as_mut() {
        child.kill().map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn spawn_json_process(
    app: AppHandle,
    state: &SeparationState,
    mut command: Command,
    prefix: &'static str,
) -> Result<(), String> {
    if state.running.swap(true, Ordering::SeqCst) {
        return Err(format!("a {prefix} job is already running"));
    }
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            state.running.store(false, Ordering::SeqCst);
            return Err(format!("cannot start {prefix} worker: {error}"));
        }
    };
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| format!("{prefix} stdout is unavailable"))?;
    let stderr = child.stderr.take();
    *state.lock_child()? = Some(child);
    std::thread::spawn(move || {
        let mut terminal = false;
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            let Ok(payload) = serde_json::from_str::<Value>(&line) else {
                continue;
            };
            match payload.get("type").and_then(Value::as_str) {
                Some("progress") => {
                    let _ = app.emit(&format!("{prefix}-progress"), payload);
                }
                Some("result") => {
                    terminal = true;
                    let _ = app.emit(&format!("{prefix}-finished"), payload);
                }
                Some("error") => {
                    terminal = true;
                    let message = payload
                        .get("message")
                        .and_then(Value::as_str)
                        .unwrap_or("worker failed")
                        .to_owned();
                    let _ = app.emit(&format!("{prefix}-failed"), message);
                }
                _ => {}
            }
        }
        let status = app.state::<SeparationState>();
        let mut process = status.lock_child().ok().and_then(|mut value| value.take());
        if let Some(process) = process.as_mut() {
            let _ = process.wait();
        }
        if !terminal {
            let details = stderr
                .map(|mut stream| {
                    let mut text = String::new();
                    let _ = stream.read_to_string(&mut text);
                    text
                })
                .filter(|value| !value.trim().is_empty())
                .unwrap_or_else(|| "worker exited without a terminal message".to_owned());
            let _ = app.emit(&format!("{prefix}-failed"), details);
        }
        status.running.store(false, Ordering::SeqCst);
    });
    Ok(())
}

#[tauri::command]
pub fn separator_component_status(directory: PathBuf) -> SeparatorComponentStatus {
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
pub fn separator_component_install(
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
    let staging = parent.join(format!(
        ".{}-partial",
        target.file_name().unwrap_or_default().to_string_lossy()
    ));
    if staging.exists() {
        fs::remove_dir_all(&staging).map_err(|error| error.to_string())?;
    }
    fs::create_dir_all(&staging).map_err(|error| error.to_string())?;
    let result = (|| {
        extract_archive(&archive, &staging)?;
        inspect_component(&staging)?;
        if target.exists() {
            if !overwrite {
                return Err(format!(
                    "separator component already exists: {}",
                    target.display()
                ));
            }
            fs::remove_dir_all(&target).map_err(|error| error.to_string())?;
        }
        fs::rename(&staging, &target).map_err(|error| error.to_string())?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_dir_all(&staging);
    }
    result?;
    Ok(separator_component_status(target))
}

#[tauri::command]
pub fn separator_component_uninstall(target: PathBuf) -> Result<(), String> {
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
        statuses.push(SeparatorModelStatus {
            id: id.to_owned(),
            quality: quality.to_owned(),
            logical_sha256: logical_sha256.to_owned(),
            verified: true,
        });
    }
    Ok((manifest, statuses))
}

fn verify_file(
    root: &Path,
    relative: &str,
    expected_hash: Option<&str>,
    expected_size: Option<u64>,
) -> Result<(), String> {
    let path = root.join(relative);
    if !path.is_file() {
        return Err(format!("separator file is missing: {relative}"));
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
    fn component_install_status_and_uninstall_round_trip() {
        let root = std::env::temp_dir().join(format!("glt-separator-component-{}", Uuid::new_v4()));
        let source = root.join("source");
        fs::create_dir_all(source.join("worker/models")).unwrap();
        fs::create_dir_all(source.join("licenses")).unwrap();
        fs::write(source.join("worker/worker.exe"), b"runtime").unwrap();
        fs::write(source.join("licenses/THIRD_PARTY.txt"), b"MIT").unwrap();
        fs::write(source.join("worker/models/htdemucs.bundle.json"), b"{}").unwrap();
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
                "size_bytes": 2,
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
        ] {
            writer.start_file(relative, options).unwrap();
            writer
                .write_all(&fs::read(source.join(relative)).unwrap())
                .unwrap();
        }
        writer.finish().unwrap();

        let target = root.join("installed");
        let installed = separator_component_install(archive, target.clone(), false).unwrap();
        assert!(installed.installed);
        assert_eq!(installed.models.len(), 1);
        separator_component_uninstall(target.clone()).unwrap();
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
