use std::path::{Path, PathBuf};
use std::process::Command;

fn binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_glt"))
}

fn mock_worker() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/mock_worker.py")
}

#[test]
fn doctor_emits_only_json() {
    let output = Command::new(binary())
        .args(["doctor", "--worker"])
        .arg(mock_worker())
        .arg("--json")
        .output()
        .expect("run glt doctor");
    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let document: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("stdout must be JSON");
    assert_eq!(document["status"], "ok");
    assert_eq!(document["worker_version"], "mock-1");
}

#[test]
fn transcribe_keeps_stdout_machine_readable() {
    let unique = format!("glt-cli-test-{}", std::process::id());
    let input = std::env::temp_dir().join(format!("{unique}.wav"));
    let output_dir = std::env::temp_dir().join(unique);
    std::fs::write(&input, b"not real audio; controller test only").unwrap();

    let output = Command::new(binary())
        .args(["transcribe"])
        .arg(&input)
        .arg("--output")
        .arg(&output_dir)
        .arg("--worker")
        .arg(mock_worker())
        .arg("--bpm")
        .arg("120")
        .arg("--json")
        .output()
        .expect("run glt transcribe");
    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let document: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("stdout must contain one JSON value");
    assert_eq!(document["result"]["artifacts"][0]["kind"], "events");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("transcribing"));
    assert!(stderr.contains("MOCK_WARNING"));
}

#[test]
fn invalid_segment_is_usage_error() {
    let unique = format!("glt-cli-invalid-{}", std::process::id());
    let input = std::env::temp_dir().join(format!("{unique}.wav"));
    std::fs::write(&input, b"test").unwrap();
    let output = Command::new(binary())
        .args(["transcribe"])
        .arg(&input)
        .arg("--output")
        .arg(std::env::temp_dir().join(unique))
        .args(["--start-seconds", "2", "--end-seconds", "1"])
        .output()
        .expect("run invalid transcription");
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("end-seconds"));
}

#[test]
fn worker_failure_is_processing_error() {
    let unique = format!("glt-cli-worker-failure-{}", std::process::id());
    let input = std::env::temp_dir().join(format!("{unique}.wav"));
    std::fs::write(&input, b"test").unwrap();
    let output = Command::new(binary())
        .env("GLT_MOCK_WORKER_MODE", "error")
        .args(["transcribe"])
        .arg(&input)
        .arg("--output")
        .arg(std::env::temp_dir().join(unique))
        .arg("--worker")
        .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/mock_worker.py"))
        .output()
        .expect("run failing worker");
    assert_eq!(output.status.code(), Some(4));
    assert!(String::from_utf8_lossy(&output.stderr).contains("MOCK_FAILURE"));
}
