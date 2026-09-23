from __future__ import annotations

import io
import json
import time
from collections.abc import Callable
from typing import Any

import pytest

import glt_core.worker as worker_module
from glt_core.transcription import TranscriptionCancelled
from glt_core.worker import WorkerServer


class MemoryInput:
    def __init__(self, lines: list[bytes]) -> None:
        self.buffer = io.BytesIO(b"".join(lines))


class FakeServer(WorkerServer):
    def __init__(self, lines: list[bytes], output: io.StringIO) -> None:
        super().__init__(MemoryInput(lines), output)

    def _transcribe(
        self,
        payload: dict[str, Any],
        job_id: str,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        self._writer.send(
            kind="progress",
            job_id=job_id,
            payload={"stage": "transcribing", "fraction": 0.5},
        )
        return {
            "output_dir": payload["staging_dir"],
            "report_path": "report.json",
            "artifacts": [],
        }


class CancelServer(FakeServer):
    def _transcribe(
        self,
        payload: dict[str, Any],
        job_id: str,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        self._writer.send(
            kind="progress",
            job_id=job_id,
            payload={"stage": "transcribing", "fraction": 0.5},
        )
        deadline = time.monotonic() + 2
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise TranscriptionCancelled()


def _message(kind: str, **payload: object) -> bytes:
    return (
        json.dumps(
            {
                "protocol_version": 1,
                "job_id": "local-test",
                "type": kind,
                "payload": payload,
            }
        )
        + "\n"
    ).encode()


def _outputs(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.fixture(autouse=True)
def fake_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "load_basic_pitch_runtime", lambda _path: object())


def test_worker_ready_and_result() -> None:
    output = io.StringIO()
    start = _message(
        "start",
        operation="transcribe",
        input_path="input.mp4",
        staging_dir="staging",
        options={},
    )
    assert FakeServer([start], output).run() == 0
    messages = _outputs(output)
    assert [message["type"] for message in messages] == ["ready", "progress", "result"]
    assert messages[0]["job_id"] is None
    assert messages[-1]["job_id"] == "local-test"


def test_worker_cancellation() -> None:
    output = io.StringIO()
    start = _message(
        "start",
        operation="transcribe",
        input_path="input.mp4",
        staging_dir="staging",
        options={},
    )
    cancel = _message("cancel")
    assert CancelServer([start, cancel], output).run() == 0
    assert [message["type"] for message in _outputs(output)] == [
        "ready",
        "progress",
        "cancelled",
    ]


def test_worker_rejects_version_and_long_line() -> None:
    output = io.StringIO()
    message = {
        "protocol_version": 2,
        "job_id": "local-test",
        "type": "cancel",
        "payload": {},
    }
    assert FakeServer([(json.dumps(message) + "\n").encode()], output).run() == 0
    assert _outputs(output)[-1]["payload"]["code"] == "UNSUPPORTED_VERSION"

    output = io.StringIO()
    assert FakeServer([b"x" * (worker_module.MAX_LINE_BYTES + 1) + b"\n"], output).run() == 1
    assert _outputs(output)[-1]["payload"]["code"] == "LINE_TOO_LONG"
