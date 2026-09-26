from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

mode = os.environ.get("GLT_MOCK_WORKER_MODE", sys.argv[1] if len(sys.argv) > 1 else "normal")
job_id: str | None = None


def send(value: dict[str, object]) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def envelope(kind: str, payload: dict[str, object], *, ready: bool = False) -> dict[str, object]:
    return {
        "protocol_version": 1 if mode == "wrong-version" else 3,
        "job_id": None if ready else job_id,
        "type": kind,
        "payload": payload,
    }


def result_payload(staging_dir: str) -> dict[str, object]:
    relative_path = "missing.events.json" if mode == "missing-artifact" else "score.events.json"
    artifact = pathlib.Path(staging_dir) / relative_path
    if mode != "missing-artifact":
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            '{"events":[],"format_version":1,"time_unit":"us","duration_us":0}\n',
            encoding="utf-8",
        )
    data = artifact.read_bytes() if artifact.is_file() else b""
    sha256 = hashlib.sha256(data).hexdigest()
    size_bytes = len(data)
    if mode == "bad-hash":
        sha256 = "0" * 64
    if mode == "bad-size":
        size_bytes += 1
    report = pathlib.Path(staging_dir) / "report.json"
    report.write_text('{"schema_version":1}\n', encoding="utf-8")
    report_data = report.read_bytes()
    return {
        "output_dir": staging_dir,
        "report_path": "report.json",
        "artifacts": [
            {
                "kind": "events",
                "relative_path": relative_path,
                "sha256": sha256,
                "size_bytes": size_bytes,
            },
            {
                "kind": "report",
                "relative_path": "report.json",
                "sha256": hashlib.sha256(report_data).hexdigest(),
                "size_bytes": len(report_data),
            },
        ],
    }


send(
    envelope(
        "ready",
        {
            "worker_version": "mock-1",
            "application_version": "0.1.0",
            "model_version": "mock-model",
        },
        ready=True,
    )
)

for raw_line in sys.stdin:
    try:
        message = json.loads(raw_line)
    except json.JSONDecodeError:
        continue
    if message.get("type") != "start":
        continue
    job_id = str(message["job_id"])
    options = message["payload"]["options"]
    staging_dir = str(message["payload"]["staging_dir"])
    echo_path = os.environ.get("GLT_MOCK_WORKER_ECHO")
    if echo_path:
        pathlib.Path(echo_path).write_text(
            json.dumps(
                {
                    "options": options,
                    "env": {
                        key: os.environ.get(key)
                        for key in (
                            "GLT_CLEANING_PROFILE",
                            "GLT_MIN_CONFIDENCE",
                            "GLT_MIN_DURATION_US",
                            "GLT_RETRIGGER_GAP_US",
                            "GLT_ARRANGEMENT",
                            "GLT_ONSET_WINDOW_US",
                            "GLT_MAX_VOICES",
                        )
                    },
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    if mode == "long-line":
        print("x" * (1024 * 1024 + 100), flush=True)
        time.sleep(30)
    elif mode == "invalid-json":
        print("{not-json", flush=True)
        time.sleep(30)
    elif mode == "crash":
        os._exit(7)
    elif mode == "stderr-flood":
        sys.stderr.write("e" * 200_000)
        sys.stderr.flush()
        os._exit(7)
    elif mode == "wrong-job":
        send(
            {
                "protocol_version": 3,
                "job_id": "other-job",
                "type": "result",
                "payload": result_payload(staging_dir),
            }
        )
    elif mode == "duplicate-terminal":
        send(envelope("result", result_payload(staging_dir)))
        send(envelope("result", result_payload(staging_dir)))
    elif mode == "invalid-progress":
        send(envelope("progress", {"stage": "guessing", "fraction": 0.5}))
        time.sleep(30)
    elif mode == "error":
        send(
            envelope(
                "error",
                {
                    "code": "MOCK_FAILURE",
                    "message": "mock processing failure",
                    "retryable": False,
                },
            )
        )
    elif mode == "unsafe-path":
        payload = result_payload(staging_dir)
        payload["artifacts"][0]["relative_path"] = "../escape.events.json"
        send(envelope("result", payload))
    elif mode == "cancel-ignore":
        send(envelope("progress", {"stage": "transcribing", "fraction": 0.1}))
        while True:
            time.sleep(1)
    elif mode == "cancel-tree":
        marker = sys.argv[2]
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import pathlib, sys, time; "
                "time.sleep(3); "
                "pathlib.Path(sys.argv[1]).write_text('alive')",
                marker,
            ]
        )
        send(envelope("progress", {"stage": "transcribing", "fraction": 0.1}))
        while True:
            time.sleep(1)
    elif mode == "cancel":
        send(envelope("progress", {"stage": "transcribing", "fraction": 0.1}))
        for cancel_line in sys.stdin:
            cancel = json.loads(cancel_line)
            if cancel.get("type") == "cancel":
                send(envelope("cancelled", {}))
                raise SystemExit(0)
    else:
        send(envelope("progress", {"stage": "transcribing", "fraction": 0.25}))
        send(
            envelope(
                "warning",
                {"code": "MOCK_WARNING", "message": "mock warning"},
            )
        )
        send(envelope("result", result_payload(staging_dir)))
        if options.get("overwrite"):
            time.sleep(0.01)
