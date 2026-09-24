use std::path::{Path, PathBuf};
use std::process::Command;

fn binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_glt"))
}

fn mock_worker() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/mock_worker.py")
}

fn unique_test_dir(name: &str) -> PathBuf {
    std::env::temp_dir().join(format!("glt-cli-{name}-{}", std::process::id()))
}

fn run_mock_transcribe(
    mode: &str,
    input: &Path,
    output: &Path,
    overwrite: bool,
) -> std::process::Output {
    let mut command = Command::new(binary());
    command
        .env("GLT_MOCK_WORKER_MODE", mode)
        .args(["transcribe"])
        .arg(input)
        .arg("--output")
        .arg(output)
        .arg("--worker")
        .arg(mock_worker());
    if overwrite {
        command.arg("--overwrite");
    }
    command.output().expect("run glt transcribe")
}

fn prepare_case(name: &str) -> (PathBuf, PathBuf, PathBuf) {
    let root = unique_test_dir(name);
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).unwrap();
    let input = root.join("input.wav");
    let output = root.join("result");
    std::fs::write(&input, b"mock input bytes").unwrap();
    (root, input, output)
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

#[test]
fn publishes_verified_staging_and_preserves_source_input() {
    let (root, input, output) = prepare_case("publish");
    let before = std::fs::read(&input).unwrap();

    let result = run_mock_transcribe("normal", &input, &output, false);
    assert!(
        result.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(output.join("score.events.json").is_file());
    assert_eq!(std::fs::read(&input).unwrap(), before);
    assert!(std::fs::read_dir(&root).unwrap().all(|entry| {
        !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".glt-")
    }));
}

#[test]
fn existing_output_requires_overwrite_and_replaces_as_unit() {
    let (root, input, output) = prepare_case("overwrite");
    std::fs::create_dir_all(&output).unwrap();
    std::fs::write(output.join("old.txt"), b"keep").unwrap();

    let blocked = run_mock_transcribe("normal", &input, &output, false);
    assert_eq!(blocked.status.code(), Some(5));
    assert_eq!(std::fs::read(output.join("old.txt")).unwrap(), b"keep");

    let replaced = run_mock_transcribe("normal", &input, &output, true);
    assert!(
        replaced.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&replaced.stderr)
    );
    assert!(output.join("score.events.json").is_file());
    assert!(!output.join("old.txt").exists());
    assert_eq!(std::fs::read(&input).unwrap(), b"mock input bytes");
    assert!(std::fs::read_dir(&root).unwrap().all(|entry| {
        !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".glt-")
    }));
}

#[test]
fn rejects_untrusted_artifacts_without_publishing_output() {
    for mode in ["missing-artifact", "bad-hash", "bad-size"] {
        let (_root, input, output) = prepare_case(mode);
        let before = std::fs::read(&input).unwrap();
        let result = run_mock_transcribe(mode, &input, &output, false);
        assert_eq!(
            result.status.code(),
            Some(5),
            "mode {mode}: {}",
            String::from_utf8_lossy(&result.stderr)
        );
        assert!(!output.exists(), "mode {mode} published a partial output");
        assert_eq!(std::fs::read(&input).unwrap(), before);
    }
}

#[test]
fn refuses_to_overwrite_an_output_containing_the_source_input() {
    let root = unique_test_dir("input-inside-output");
    let _ = std::fs::remove_dir_all(&root);
    let output = root.join("result");
    std::fs::create_dir_all(&output).unwrap();
    let input = output.join("input.wav");
    std::fs::write(&input, b"source").unwrap();

    let result = run_mock_transcribe("normal", &input, &output, true);
    assert_eq!(result.status.code(), Some(2));
    assert_eq!(std::fs::read(&input).unwrap(), b"source");
}

#[test]
fn invalid_cleaning_threshold_is_usage_error() {
    let unique = format!("glt-cli-cleaning-{}", std::process::id());
    let input = std::env::temp_dir().join(format!("{unique}.wav"));
    std::fs::write(&input, b"test").unwrap();
    let output = Command::new(binary())
        .args(["transcribe"])
        .arg(&input)
        .arg("--output")
        .arg(std::env::temp_dir().join(unique))
        .args(["--min-confidence", "1.5"])
        .output()
        .expect("run invalid cleaning options");
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("min-confidence"));
}

#[test]
fn invalid_arrangement_options_are_usage_errors() {
    for (argument, value, expected) in [
        ("--onset-window-ms", "0", "onset-window-ms"),
        ("--max-voices", "22", "max-voices"),
    ] {
        let unique = format!("glt-cli-arrangement-{}", std::process::id());
        let input = std::env::temp_dir().join(format!("{unique}.wav"));
        std::fs::write(&input, b"test").unwrap();
        let output = Command::new(binary())
            .args(["transcribe"])
            .arg(&input)
            .arg("--output")
            .arg(std::env::temp_dir().join(unique))
            .args([argument, value])
            .output()
            .expect("run invalid arrangement options");
        assert_eq!(output.status.code(), Some(2));
        assert!(String::from_utf8_lossy(&output.stderr).contains(expected));
    }
}

#[test]
fn filter_accepts_a_v2_filter_spec_and_result_directory() {
    let root = unique_test_dir("filter-command");
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).unwrap();
    let source = root.join("result");
    std::fs::create_dir_all(&source).unwrap();
    let filter = root.join("filter.json");
    std::fs::write(
        &filter,
        br#"{"format_version":1,"rules":[{"enabled":true,"duration_ms":{"min":80,"max":1000}}]}"#,
    )
    .unwrap();
    let output_dir = root.join("filtered");
    let output = Command::new(binary())
        .args(["filter"])
        .arg(&source)
        .arg("--output")
        .arg(&output_dir)
        .arg("--filter-file")
        .arg(&filter)
        .arg("--worker")
        .arg(mock_worker())
        .output()
        .expect("run glt filter");
    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(output_dir.is_dir());
}

#[test]
fn invalid_filter_file_is_usage_error() {
    let root = unique_test_dir("filter-invalid");
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).unwrap();
    let source = root.join("result");
    std::fs::create_dir_all(&source).unwrap();
    let filter = root.join("filter.json");
    std::fs::write(
        &filter,
        br#"{"format_version":1,"rules":[{"enabled":true,"velocity":{"min":0,"max":127}}]}"#,
    )
    .unwrap();
    let output = Command::new(binary())
        .args(["filter"])
        .arg(&source)
        .arg("--output")
        .arg(root.join("filtered"))
        .arg("--filter-file")
        .arg(&filter)
        .output()
        .expect("run invalid filter");
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("velocity"));
}

#[test]
fn preview_reports_missing_audio_without_opening_device() {
    let root = unique_test_dir("preview-missing");
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).unwrap();
    let report = serde_json::json!({"artifacts": []});
    std::fs::write(
        root.join("report.json"),
        serde_json::to_vec(&report).unwrap(),
    )
    .unwrap();

    let output = Command::new(binary())
        .arg("preview")
        .arg(&root)
        .output()
        .expect("run glt preview");
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("--preview-wav"));
}
