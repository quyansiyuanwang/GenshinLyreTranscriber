use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use sha2::{Digest, Sha256};

pub fn prepare(input: &Path, worker_path: Option<&Path>) -> Result<PathBuf, String> {
    if !input.is_file() {
        return Err(format!("playback source is missing: {}", input.display()));
    }
    let cache_root = std::env::temp_dir().join("glt-ab-cache");
    fs::create_dir_all(&cache_root).map_err(|error| error.to_string())?;
    let target = cache_root.join(cache_key(input)?).join("source.wav");
    if target.is_file() && target.metadata().is_ok_and(|metadata| metadata.len() > 44) {
        return Ok(target);
    }
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    let staging = target.with_extension(format!("partial-{}.wav", uuid::Uuid::new_v4()));
    let ffmpeg = find_ffmpeg(worker_path)?;
    let mut command = Command::new(ffmpeg);
    command
        .arg("-hide_banner")
        .arg("-loglevel")
        .arg("error")
        .arg("-y")
        .arg("-i")
        .arg(input)
        .arg("-vn")
        .arg("-ac")
        .arg("2")
        .arg("-ar")
        .arg("44100")
        .arg("-c:a")
        .arg("pcm_s16le")
        .arg(&staging);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    let output = command.output().map_err(|error| error.to_string())?;
    if !output.status.success() {
        let _ = fs::remove_file(&staging);
        let message = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        return Err(if message.is_empty() {
            "FFmpeg failed to decode the playback source".to_owned()
        } else {
            message
        });
    }
    if !staging.is_file()
        || staging
            .metadata()
            .is_ok_and(|metadata| metadata.len() <= 44)
    {
        let _ = fs::remove_file(&staging);
        return Err("FFmpeg produced an empty playback source".to_owned());
    }
    fs::rename(&staging, &target).map_err(|error| error.to_string())?;
    Ok(target)
}

fn cache_key(input: &Path) -> Result<String, String> {
    let metadata = fs::metadata(input).map_err(|error| error.to_string())?;
    let mut hasher = Sha256::new();
    hasher.update(input.to_string_lossy().as_bytes());
    hasher.update(metadata.len().to_le_bytes());
    if let Ok(modified) = metadata.modified()
        && let Ok(duration) = modified.duration_since(std::time::UNIX_EPOCH)
    {
        hasher.update(duration.as_nanos().to_le_bytes());
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn find_ffmpeg(worker_path: Option<&Path>) -> Result<PathBuf, String> {
    let mut candidates = Vec::new();
    if let Some(worker) = worker_path
        && let Some(parent) = worker.parent()
    {
        candidates.push(parent.join("_internal/ffmpeg/bin/ffmpeg.exe"));
        candidates.push(parent.join("ffmpeg.exe"));
    }
    candidates.push(PathBuf::from("ffmpeg"));
    if let Some(path) = candidates.iter().find(|candidate| candidate.is_file()) {
        return Ok(path.clone());
    }
    if worker_path.is_some()
        && let Some(path) = candidates.first()
    {
        return Ok(path.clone());
    }
    Ok(PathBuf::from("ffmpeg"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cache_key_tracks_path_size_and_timestamp() {
        let root = std::env::temp_dir().join(format!("glt-ab-key-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("source.wav");
        fs::write(&path, b"1234").unwrap();
        let first = cache_key(&path).unwrap();
        fs::write(&path, b"12345").unwrap();
        let second = cache_key(&path).unwrap();
        assert_ne!(first, second);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn bundled_ffmpeg_is_preferred_over_path_lookup() {
        let worker = Path::new("C:/app/glt-worker/glt-worker.exe");
        let result = find_ffmpeg(Some(worker)).unwrap();
        let expected = Path::new("_internal")
            .join("ffmpeg")
            .join("bin")
            .join("ffmpeg.exe");
        assert!(result.ends_with(expected));
    }

    #[test]
    #[ignore = "requires GLT_TEST_FLAC and GLT_TEST_WORKER"]
    fn prepare_decodes_flac_with_bundled_ffmpeg() {
        let input = std::env::var_os("GLT_TEST_FLAC").expect("GLT_TEST_FLAC");
        let worker = std::env::var_os("GLT_TEST_WORKER").expect("GLT_TEST_WORKER");
        let output = prepare(Path::new(&input), Some(Path::new(&worker))).unwrap();
        assert!(output.is_file());
        assert_eq!(
            output.extension().and_then(|value| value.to_str()),
            Some("wav")
        );
        assert!(output.metadata().unwrap().len() > 44);
    }
}
