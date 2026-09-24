use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};
use std::time::Duration;

use glt::jobs::{
    CancellationOutcome, MAX_STDERR_BYTES, Operation, StartOptions, StartRequest, Transpose,
    WorkerClient, WorkerError, WorkerEvent, WorkerSpec,
};

fn mock_worker(mode: &str) -> WorkerSpec {
    mock_worker_with_args(mode, std::iter::empty::<&str>())
}

fn mock_worker_with_args<'a>(mode: &str, extra: impl IntoIterator<Item = &'a str>) -> WorkerSpec {
    let python = std::env::var_os("PYTHON").unwrap_or_else(|| OsStr::new("python").to_os_string());
    let script = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/mock_worker.py");
    let mut args = vec![
        OsString::from("-u"),
        script.into_os_string(),
        OsString::from(mode),
    ];
    args.extend(extra.into_iter().map(OsString::from));
    WorkerSpec::new(PathBuf::from(python)).with_args(args)
}

fn request(staging: &Path) -> StartRequest {
    StartRequest {
        operation: Operation::Transcribe,
        input_path: PathBuf::from("input with spaces & symbols.wav"),
        staging_dir: staging.to_path_buf(),
        options: StartOptions {
            timing: None,
            bpm: Some(120.0),
            transpose: Some(Transpose::automatic()),
            audio_track: None,
            start_us: None,
            end_us: None,
            preview_wav: Some(false),
            overwrite: Some(true),
            mapping_profile: None,
            filter: None,
            filter_preset: None,
        },
    }
}

fn launch(mode: &str) -> WorkerClient {
    WorkerClient::launch(mock_worker(mode), Duration::from_secs(2)).expect("worker ready")
}

fn test_staging(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!("glt-controller-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&path);
    path
}

#[test]
fn receives_progress_warning_and_result() {
    let staging = std::env::temp_dir().join("glt controller normal");
    let mut client = launch("normal");
    client.start("local-test-job", &request(&staging)).unwrap();

    assert!(matches!(
        client.recv_event(Duration::from_secs(2)).unwrap(),
        WorkerEvent::Progress { .. }
    ));
    assert!(matches!(
        client.recv_event(Duration::from_secs(2)).unwrap(),
        WorkerEvent::Warning { .. }
    ));
    let WorkerEvent::Result(result) = client.recv_event(Duration::from_secs(2)).unwrap() else {
        panic!("expected result");
    };
    assert_eq!(result.output_dir, staging);
    assert_eq!(result.artifacts.len(), 2);
}

#[test]
fn rejects_wrong_job() {
    let mut client = launch("wrong-job");
    client
        .start("local-test-job", &request(&test_staging("wrong-job")))
        .unwrap();
    let error = client.recv_event(Duration::from_secs(2)).unwrap_err();
    assert!(matches!(
        error,
        WorkerError::Protocol {
            code: "JOB_MISMATCH",
            ..
        }
    ));
}

#[test]
fn rejects_long_line_and_invalid_json() {
    let mut long_line = launch("long-line");
    long_line
        .start("local-test-job", &request(&test_staging("long-line")))
        .unwrap();
    assert!(matches!(
        long_line.recv_event(Duration::from_secs(2)).unwrap_err(),
        WorkerError::Protocol {
            code: "LINE_TOO_LONG",
            ..
        }
    ));

    let mut invalid_json = launch("invalid-json");
    invalid_json
        .start("local-test-job", &request(&test_staging("invalid-json")))
        .unwrap();
    assert!(matches!(
        invalid_json.recv_event(Duration::from_secs(2)).unwrap_err(),
        WorkerError::Json(_)
    ));
}

#[test]
fn rejects_duplicate_terminal_and_crash() {
    let mut duplicate = launch("duplicate-terminal");
    duplicate
        .start(
            "local-test-job",
            &request(&test_staging("duplicate-terminal")),
        )
        .unwrap();
    assert!(matches!(
        duplicate.recv_event(Duration::from_secs(2)).unwrap(),
        WorkerEvent::Result(_)
    ));
    assert!(matches!(
        duplicate.recv_event(Duration::from_secs(2)).unwrap_err(),
        WorkerError::Protocol {
            code: "DUPLICATE_TERMINAL",
            ..
        }
    ));

    let mut crash = launch("crash");
    crash
        .start("local-test-job", &request(&test_staging("crash")))
        .unwrap();
    assert!(matches!(
        crash.recv_event(Duration::from_secs(2)).unwrap_err(),
        WorkerError::Exited { .. }
    ));

    let mut stderr_flood = launch("stderr-flood");
    stderr_flood
        .start("local-test-job", &request(&test_staging("stderr-flood")))
        .unwrap();
    assert!(matches!(
        stderr_flood.recv_event(Duration::from_secs(2)).unwrap_err(),
        WorkerError::Exited { .. }
    ));
    assert_eq!(stderr_flood.stderr_text().len(), MAX_STDERR_BYTES);
}

#[test]
fn rejects_invalid_progress_and_unsafe_path() {
    let mut progress = launch("invalid-progress");
    progress
        .start(
            "local-test-job",
            &request(&test_staging("invalid-progress")),
        )
        .unwrap();
    assert!(matches!(
        progress.recv_event(Duration::from_secs(2)).unwrap_err(),
        WorkerError::Protocol {
            code: "SCHEMA_INVALID",
            ..
        }
    ));

    let mut path = launch("unsafe-path");
    path.start("local-test-job", &request(&test_staging("unsafe-path")))
        .unwrap();
    assert!(matches!(
        path.recv_event(Duration::from_secs(2)).unwrap_err(),
        WorkerError::Protocol {
            code: "UNSAFE_PATH",
            ..
        }
    ));
}

#[test]
fn cancels_worker_and_forced_cleanup() {
    let mut cooperative = launch("cancel");
    cooperative
        .start("local-test-job", &request(&test_staging("cancel")))
        .unwrap();
    assert!(matches!(
        cooperative.recv_event(Duration::from_secs(2)).unwrap(),
        WorkerEvent::Progress { .. }
    ));
    assert_eq!(
        cooperative.cancel(Duration::from_secs(2)).unwrap(),
        CancellationOutcome::WorkerCancelled
    );
    assert!(!cooperative.is_running().unwrap());

    let mut stubborn = launch("cancel-ignore");
    stubborn
        .start("local-test-job", &request(&test_staging("cancel-ignore")))
        .unwrap();
    assert!(matches!(
        stubborn.recv_event(Duration::from_secs(2)).unwrap(),
        WorkerEvent::Progress { .. }
    ));
    assert_eq!(
        stubborn.cancel(Duration::from_millis(200)).unwrap(),
        CancellationOutcome::Forced
    );
    assert!(!stubborn.is_running().unwrap());

    let marker = std::env::temp_dir().join(format!("glt-tree-marker-{}", std::process::id()));
    let _ = std::fs::remove_file(&marker);
    let mut tree = WorkerClient::launch(
        mock_worker_with_args("cancel-tree", [marker.to_str().unwrap()]),
        Duration::from_secs(2),
    )
    .unwrap();
    tree.start("local-test-job", &request(&test_staging("cancel-tree")))
        .unwrap();
    assert!(matches!(
        tree.recv_event(Duration::from_secs(2)).unwrap(),
        WorkerEvent::Progress { .. }
    ));
    assert_eq!(
        tree.cancel(Duration::from_millis(200)).unwrap(),
        CancellationOutcome::Forced
    );
    std::thread::sleep(Duration::from_millis(3_300));
    assert!(
        !marker.exists(),
        "descendant process survived tree cancellation"
    );
}

#[test]
fn preserves_special_character_paths_without_shell() {
    let staging = std::env::temp_dir().join("glt staging with spaces & () [x]");
    let mut client = launch("normal");
    client.start("local-test-job", &request(&staging)).unwrap();
    let result = loop {
        if let WorkerEvent::Result(result) = client.recv_event(Duration::from_secs(2)).unwrap() {
            break result;
        }
    };
    assert_eq!(result.output_dir, staging);
}
