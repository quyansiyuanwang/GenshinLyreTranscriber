use std::fs::File;
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};

use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager, State};

const ANALYSIS_MANIFEST: &str = "analysis-manifest-v1.json";

#[derive(Default)]
pub struct AnalysisState {
    running: AtomicBool,
    child: Mutex<Option<Child>>,
}

impl AnalysisState {
    fn lock_child(&self) -> Result<std::sync::MutexGuard<'_, Option<Child>>, String> {
        self.child
            .lock()
            .map_err(|_| "analysis process state is unavailable".to_owned())
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AnalysisRequest {
    pub input: PathBuf,
    pub output: PathBuf,
    pub audio_track: Option<u32>,
    pub start_us: Option<u64>,
    pub end_us: Option<u64>,
    pub fft_size: u32,
    pub hop_size: u32,
    pub window: String,
    pub spectral: bool,
    pub worker_path: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize)]
pub struct WaveformPayload {
    pub samples_per_bucket: u64,
    pub bucket_count: u64,
    pub data_base64: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SpectrogramImage {
    pub width: u32,
    pub height: u32,
    pub data_base64: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SpectrumFrame {
    pub frame: u64,
    pub total_frames: u64,
    pub hop_us: u64,
    pub spectrum: Vec<u8>,
    pub features: Vec<f32>,
    pub columns: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PlaybackStatus {
    pub position_us: u64,
    pub paused: bool,
    pub available: bool,
}

#[tauri::command]
pub fn start_analysis(
    app: AppHandle,
    state: State<'_, AnalysisState>,
    request: AnalysisRequest,
) -> Result<(), String> {
    if state.running.swap(true, Ordering::SeqCst) {
        return Err("an analysis job is already running".to_owned());
    }
    let spec = glt::desktop::worker_spec(request.worker_path.clone())?;
    let mut command = Command::new(&spec.program);
    command.args(&spec.args);
    command.envs(spec.env);
    if let Some(directory) = spec.working_directory {
        command.current_dir(directory);
    }
    command.arg("analyze");
    command.arg("--input").arg(&request.input);
    command.arg("--output").arg(&request.output);
    if let Some(track) = request.audio_track {
        command.arg("--audio-track").arg(track.to_string());
    }
    if let Some(start) = request.start_us {
        command.arg("--start-us").arg(start.to_string());
    }
    if let Some(end) = request.end_us {
        command.arg("--end-us").arg(end.to_string());
    }
    command
        .arg("--fft-size")
        .arg(request.fft_size.to_string())
        .arg("--hop-size")
        .arg(request.hop_size.to_string())
        .arg("--window")
        .arg(&request.window);
    if request.spectral {
        command.arg("--spectral");
    }
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    if cfg!(windows) {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    let mut child = command
        .spawn()
        .map_err(|error| format!("cannot start analysis worker: {error}"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "analysis stdout is unavailable".to_owned())?;
    *state.lock_child()? = Some(child);

    std::thread::spawn(move || {
        let mut failed = false;
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else {
                failed = true;
                break;
            };
            let Ok(payload) = serde_json::from_str::<Value>(&line) else {
                failed = true;
                continue;
            };
            match payload.get("type").and_then(Value::as_str) {
                Some("progress") => {
                    let _ = app.emit("analysis-progress", payload);
                }
                Some("result") => {
                    let _ = app.emit(
                        "analysis-progress",
                        serde_json::json!({
                            "type": "progress",
                            "stage": "completed",
                            "fraction": 1.0
                        }),
                    );
                    let _ = app.emit("analysis-finished", payload);
                }
                Some("error") => {
                    failed = true;
                    let _ = app.emit(
                        "analysis-failed",
                        payload
                            .get("message")
                            .and_then(Value::as_str)
                            .unwrap_or("analysis failed"),
                    );
                }
                _ => {}
            }
        }

        let state = app.state::<AnalysisState>();
        let mut guard = match state.lock_child() {
            Ok(guard) => guard,
            Err(_) => return,
        };
        let status = guard.as_mut().and_then(|child| child.wait().ok());
        let mut stderr = String::new();
        if let Some(mut child) = guard.take()
            && let Some(mut stream) = child.stderr.take()
            && stream.read_to_string(&mut stderr).is_ok()
            && !stderr.trim().is_empty()
        {
            failed = true;
            let _ = app.emit("analysis-failed", stderr);
        }
        drop(guard);
        if !failed && !status.is_some_and(|value| value.success()) {
            let _ = app.emit("analysis-failed", "analysis worker exited unsuccessfully");
        }
        state.running.store(false, Ordering::SeqCst);
    });
    Ok(())
}

#[tauri::command]
pub fn cancel_analysis(state: State<'_, AnalysisState>) -> Result<(), String> {
    let mut guard = state.lock_child()?;
    if let Some(child) = guard.as_mut() {
        child
            .kill()
            .map_err(|error| format!("cannot cancel analysis: {error}"))?;
    }
    Ok(())
}

#[tauri::command]
pub fn analysis_manifest(directory: PathBuf) -> Result<Value, String> {
    read_json(&directory.join(ANALYSIS_MANIFEST))
}

#[tauri::command]
pub fn analysis_waveform(directory: PathBuf, level: usize) -> Result<WaveformPayload, String> {
    let manifest = read_json(&directory.join(ANALYSIS_MANIFEST))?;
    let levels = manifest
        .pointer("/waveform/levels")
        .and_then(Value::as_array)
        .ok_or_else(|| "analysis manifest has no waveform levels".to_owned())?;
    let selected = levels
        .get(level)
        .ok_or_else(|| format!("waveform level {level} does not exist"))?;
    let offset = selected
        .get("offset_bytes")
        .and_then(Value::as_u64)
        .ok_or_else(|| "waveform offset is invalid".to_owned())?;
    let size = selected
        .get("size_bytes")
        .and_then(Value::as_u64)
        .ok_or_else(|| "waveform size is invalid".to_owned())?;
    let bucket_count = selected
        .get("bucket_count")
        .and_then(Value::as_u64)
        .ok_or_else(|| "waveform bucket count is invalid".to_owned())?;
    let samples_per_bucket = selected
        .get("samples_per_bucket")
        .and_then(Value::as_u64)
        .ok_or_else(|| "waveform samples per bucket is invalid".to_owned())?;
    let path = artifact_path(&directory, &manifest, "waveform")?;
    let mut file = File::open(path).map_err(|error| error.to_string())?;
    file.seek(SeekFrom::Start(offset))
        .map_err(|error| error.to_string())?;
    let mut data =
        vec![0_u8; usize::try_from(size).map_err(|_| "waveform is too large".to_owned())?];
    file.read_exact(&mut data)
        .map_err(|error| error.to_string())?;
    Ok(WaveformPayload {
        samples_per_bucket,
        bucket_count,
        data_base64: BASE64.encode(data),
    })
}

#[tauri::command]
pub fn analysis_spectrogram_image(
    directory: PathBuf,
    width: u32,
    height: u32,
) -> Result<SpectrogramImage, String> {
    if width == 0 || height == 0 || width > 4096 || height > 4096 {
        return Err("spectrogram image size is invalid".to_owned());
    }
    let manifest = read_json(&directory.join(ANALYSIS_MANIFEST))?;
    let frames = field_u64(&manifest, "/spectral/frames")?;
    let bins = field_u64(&manifest, "/spectral/bins")?;
    let path = artifact_path(&directory, &manifest, "spectrogram")?;
    let raw = std::fs::read(path).map_err(|error| error.to_string())?;
    if raw.len() as u64 != frames * bins {
        return Err("spectrogram file size does not match manifest".to_owned());
    }
    let mut image = vec![0_u8; width as usize * height as usize];
    for y in 0..height as u64 {
        let source_frame = y * frames / height as u64;
        for x in 0..width as u64 {
            let source_bin = x * bins / width as u64;
            image[(y * width as u64 + x) as usize] =
                raw[(source_frame * bins + source_bin) as usize];
        }
    }
    Ok(SpectrogramImage {
        width,
        height,
        data_base64: BASE64.encode(image),
    })
}

#[tauri::command]
pub fn analysis_spectrum(directory: PathBuf, frame: Option<u64>) -> Result<SpectrumFrame, String> {
    let manifest = read_json(&directory.join(ANALYSIS_MANIFEST))?;
    let frames = field_u64(&manifest, "/spectral/frames")?;
    let bins = field_u64(&manifest, "/spectral/bins")?;
    let selected = frame.unwrap_or(0).min(frames.saturating_sub(1));
    let spectrogram = artifact_path(&directory, &manifest, "spectrogram")?;
    let mut file = File::open(spectrogram).map_err(|error| error.to_string())?;
    file.seek(SeekFrom::Start(selected * bins))
        .map_err(|error| error.to_string())?;
    let mut row = vec![0_u8; bins as usize];
    file.read_exact(&mut row)
        .map_err(|error| error.to_string())?;
    let target_bins = 512_usize;
    let spectrum = if row.len() <= target_bins {
        row
    } else {
        (0..target_bins)
            .map(|index| row[index * row.len() / target_bins])
            .collect()
    };

    let columns = manifest
        .pointer("/spectral/features/columns")
        .and_then(Value::as_array)
        .map(|values| {
            values
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_else(Vec::new);
    let features_path = artifact_path(&directory, &manifest, "features")?;
    let mut features = Vec::new();
    if !columns.is_empty() {
        let mut feature_file = File::open(features_path).map_err(|error| error.to_string())?;
        feature_file
            .seek(SeekFrom::Start(selected * columns.len() as u64 * 4))
            .map_err(|error| error.to_string())?;
        let mut bytes = vec![0_u8; columns.len() * 4];
        feature_file
            .read_exact(&mut bytes)
            .map_err(|error| error.to_string())?;
        features.extend(
            bytes
                .chunks_exact(4)
                .map(|chunk| f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]])),
        );
    }
    Ok(SpectrumFrame {
        frame: selected,
        total_frames: frames,
        hop_us: field_u64(&manifest, "/spectral/features/hop_us")?,
        spectrum,
        features,
        columns,
    })
}

fn read_json(path: &Path) -> Result<Value, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|error| format!("cannot read {}: {error}", path.display()))?;
    serde_json::from_str(&text).map_err(|error| format!("invalid JSON {}: {error}", path.display()))
}

fn field_u64(document: &Value, pointer: &str) -> Result<u64, String> {
    document
        .pointer(pointer)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("analysis manifest field {pointer} is invalid"))
}

fn artifact_path(directory: &Path, manifest: &Value, kind: &str) -> Result<PathBuf, String> {
    let relative = manifest
        .get("files")
        .and_then(Value::as_array)
        .and_then(|files| {
            files.iter().find_map(|entry| {
                (entry.get("kind").and_then(Value::as_str) == Some(kind))
                    .then(|| entry.get("relative_path").and_then(Value::as_str))
                    .flatten()
            })
        })
        .ok_or_else(|| format!("analysis manifest has no {kind} artifact"))?;
    let path = directory.join(relative);
    if path.parent() != Some(directory) {
        return Err(format!("unsafe analysis artifact path: {relative}"));
    }
    Ok(path)
}
