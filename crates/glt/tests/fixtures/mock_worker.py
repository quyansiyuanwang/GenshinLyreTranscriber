from __future__ import annotations

import json
import os
import subprocess
import sys
import time

mode = os.environ.get("GLT_MOCK_WORKER_MODE", sys.argv[1] if len(sys.argv) > 1 else "normal")
job_id: str | None = None


def send(value: dict[str, object]) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def envelope(kind: str, payload: dict[str, object], *, ready: bool = False) -> dict[str, object]:
    return {
        "protocol_version": 2 if mode == "wrong-version" else 1,
        "job_id": None if ready else job_id,
        "type": kind,
        "payload": payload,
    }


def result_payload(staging_dir: str) -> dict[str, object]:
    return {
        "output_dir": staging_dir,
        "report_path": "report.json",
        "artifacts": [
            {
                "kind": "events",
                "relative_path": "score.events.json",
                "sha256": "a" * 64,
                "size_bytes": 123,
            }
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
                "protocol_version": 1,
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
