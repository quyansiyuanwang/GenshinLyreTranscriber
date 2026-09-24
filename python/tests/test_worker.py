from __future__ import annotations

import io
import json
import pathlib
import threading
import time
from collections.abc import Callable
from typing import Any

import mido
import pytest

import glt_core.worker as worker_module
from glt_core.domain.note_sequence import Note, NoteSequence, Provenance
from glt_core.transcription import TranscriptionCancelled
from glt_core.worker import WorkerServer


class MemoryInput:
    def __init__(self, lines: list[bytes]) -> None:
        self.buffer = io.BytesIO(b"".join(lines))


class ControlledInput:
    def __init__(self, lines: list[bytes]) -> None:
        self.buffer = self
        self._lines = lines
        self._release = threading.Event()

    def readline(self, _limit: int = -1) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        self._release.wait()
        return b""

    def release(self) -> None:
        self._release.set()


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
                "protocol_version": 2,
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
        "protocol_version": 1,
        "job_id": "local-test",
        "type": "cancel",
        "payload": {},
    }
    assert FakeServer([(json.dumps(message) + "\n").encode()], output).run() == 0
    assert _outputs(output)[-1]["payload"]["code"] == "UNSUPPORTED_VERSION"

    output = io.StringIO()
    assert FakeServer([b"x" * (worker_module.MAX_LINE_BYTES + 1) + b"\n"], output).run() == 1
    assert _outputs(output)[-1]["payload"]["code"] == "LINE_TOO_LONG"


def test_worker_converts_midi_to_source_copy(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "input.mid"
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    track = mido.MidiTrack()
    track.extend(
        [
            mido.Message("note_on", note=60, velocity=100, time=0),
            mido.Message("note_off", note=60, velocity=0, time=480),
        ]
    )
    midi.tracks.append(track)
    midi.save(str(source))
    staging = tmp_path / "output"
    message = _message(
        "start",
        operation="convert_midi",
        input_path=str(source),
        staging_dir=str(staging),
        options={"timing": "preserve", "transpose": 0, "preview_wav": True},
    )
    output = io.StringIO()
    controlled = ControlledInput([message])
    server = WorkerServer(controlled, output)
    thread = threading.Thread(target=server.run)
    thread.start()
    deadline = time.monotonic() + 2
    while '"type":"result"' not in output.getvalue() and time.monotonic() < deadline:
        time.sleep(0.01)
    controlled.release()
    thread.join(timeout=2)
    assert not thread.is_alive()
    result = _outputs(output)[-1]
    assert result["type"] == "result"
    assert (staging / "source.mid").read_bytes() == source.read_bytes()
    assert (staging / "cleaned.mid").is_file()
    assert (staging / "mapped.mid").is_file()
    assert (staging / "score.events.json").is_file()
    assert (staging / "score.readable.txt").is_file()
    assert (staging / "score.compat.txt").is_file()
    assert (staging / "preview.wav").is_file()
    report = json.loads((staging / "report.json").read_text(encoding="utf-8"))
    assert report["input"]["source_type"] == "midi"
    assert report["counts"]["input_notes"] == 1
    assert report["counts"]["mapped_keys"] == 1
    assert report["counts"]["compatibility_collisions"] == 0
    assert {artifact["kind"] for artifact in report["artifacts"]} >= {
        "events",
        "readable_text",
        "compat_text",
        "preview_wav",
    }
    assert "COMPATIBILITY_TAIL_OMITTED" in {warning["code"] for warning in report["warnings"]}


def test_worker_reports_compatibility_slot_collision(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "collision.mid"
    midi = mido.MidiFile(type=1, ticks_per_beat=1000)
    track = mido.MidiTrack()
    track.extend(
        [
            mido.Message("note_on", note=60, velocity=100, time=20),
            mido.Message("note_on", note=61, velocity=100, time=8),
            mido.Message("note_off", note=60, velocity=0, time=1972),
            mido.Message("note_off", note=61, velocity=0, time=1000),
        ]
    )
    midi.tracks.append(track)
    midi.save(str(source))
    staging = tmp_path / "output"
    message = _message(
        "start",
        operation="convert_midi",
        input_path=str(source),
        staging_dir=str(staging),
        options={"timing": "preserve", "transpose": 0},
    )
    output = io.StringIO()
    controlled = ControlledInput([message])
    thread = threading.Thread(target=WorkerServer(controlled, output).run)
    thread.start()
    deadline = time.monotonic() + 2
    while '"type":"result"' not in output.getvalue() and time.monotonic() < deadline:
        time.sleep(0.01)
    controlled.release()
    thread.join(timeout=2)
    assert not thread.is_alive()

    report = json.loads((staging / "report.json").read_text(encoding="utf-8"))
    assert report["counts"]["input_notes"] == 2
    assert report["counts"]["compatibility_collisions"] == 1
    assert "COMPATIBILITY_COLLISIONS" in {warning["code"] for warning in report["warnings"]}
    body = "".join(
        line
        for line in (staging / "score.compat.txt").read_text(encoding="utf-8").splitlines()
        if line.startswith("/")
    )
    assert body.replace("/", "").rstrip() == " A"


def test_worker_empty_score_does_not_create_fake_preview(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "empty.mid"
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    midi.tracks.append(mido.MidiTrack())
    midi.save(str(source))
    staging = tmp_path / "output"
    message = _message(
        "start",
        operation="convert_midi",
        input_path=str(source),
        staging_dir=str(staging),
        options={"timing": "preserve", "preview_wav": True},
    )
    output = io.StringIO()
    controlled = ControlledInput([message])
    thread = threading.Thread(target=WorkerServer(controlled, output).run)
    thread.start()
    deadline = time.monotonic() + 2
    while '"type":"result"' not in output.getvalue() and time.monotonic() < deadline:
        time.sleep(0.01)
    controlled.release()
    thread.join(timeout=2)
    assert not thread.is_alive()

    assert not (staging / "preview.wav").exists()
    report = json.loads((staging / "report.json").read_text(encoding="utf-8"))
    assert "EMPTY_PREVIEW" in {warning["code"] for warning in report["warnings"]}
    assert "preview_wav" not in {artifact["kind"] for artifact in report["artifacts"]}


def test_worker_refilters_cached_candidates_without_retranscribing(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.mid"
    midi = mido.MidiFile(type=1, ticks_per_beat=1000)
    track = mido.MidiTrack()
    track.extend(
        [
            mido.Message("note_on", note=36, velocity=120, time=0),
            mido.Message("note_on", note=64, velocity=80, time=0),
            mido.Message("note_on", note=48, velocity=100, time=0),
            mido.Message("note_off", note=36, velocity=0, time=50),
            mido.Message("note_off", note=64, velocity=0, time=450),
            mido.Message("note_off", note=48, velocity=0, time=0),
        ]
    )
    midi.tracks.append(track)
    midi.save(str(source))
    first = tmp_path / "first"
    run_worker_message(
        _message(
            "start",
            operation="convert_midi",
            input_path=str(source),
            staging_dir=str(first),
            options={"timing": "preserve", "transpose": 0, "preview_wav": False},
        )
    )
    original_source = (first / "source.mid").read_bytes()
    original_report = (first / "report.json").read_bytes()
    assert (first / "score.candidates.json").is_file()

    def fail_transcribe(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("refilter must not run Basic Pitch")

    monkeypatch.setattr(worker_module, "transcribe_to_midi", fail_transcribe)
    second = tmp_path / "second"
    run_worker_message(
        _message(
            "start",
            operation="refilter",
            input_path=str(first),
            staging_dir=str(second),
            options={
                "preview_wav": True,
                "filter": {
                    "format_version": 1,
                    "rules": [
                        {
                            "enabled": True,
                            "duration_ms": {"min": 200, "max": 1000},
                            "velocity": {"min": 1, "max": 100},
                            "pitch": {"min": 48, "max": 72},
                        }
                    ],
                },
            },
        )
    )
    report = json.loads((second / "report.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 2
    assert report["selection"]["source"] == "refilter"
    assert report["selection"]["matched_notes"] == 2
    assert report["selection"]["rule_hits"] == [2]
    assert report["counts"]["input_notes"] == 3
    assert (second / "score.candidates.json").is_file()
    assert (second / "preview.wav").is_file()
    assert (first / "source.mid").read_bytes() == original_source
    assert (first / "report.json").read_bytes() == original_report


def run_worker_message(message: bytes) -> None:
    output = io.StringIO()
    controlled = ControlledInput([message])
    thread = threading.Thread(target=WorkerServer(controlled, output).run)
    thread.start()
    deadline = time.monotonic() + 2
    while '"type":"result"' not in output.getvalue() and time.monotonic() < deadline:
        time.sleep(0.01)
    controlled.release()
    thread.join(timeout=2)
    assert not thread.is_alive()
    messages = _outputs(output)
    assert messages[-1]["type"] == "result", messages[-1]


def test_worker_protocol_output_is_ascii_even_for_non_ascii_errors() -> None:
    output = io.StringIO()
    writer = worker_module.ResponseWriter(output)
    writer.send(
        kind="error",
        job_id="local-test",
        payload={"code": "FAILURE", "message": "中文错误", "retryable": False},
    )
    output.getvalue().encode("ascii")
    assert "\\u4e2d\\u6587\\u9519\\u8bef" in output.getvalue()


def _note_sequence(notes: tuple[Note, ...], duration_us: int = 1_000_000) -> NoteSequence:
    return NoteSequence(
        duration_us=duration_us,
        notes=notes,
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance("audio", 0, "test", {}),
    )


def test_worker_reads_cleaning_thresholds_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLT_CLEANING_PROFILE", "solo")
    monkeypatch.setenv("GLT_MIN_CONFIDENCE", "0.45")
    monkeypatch.setenv("GLT_MIN_DURATION_US", "120000")
    monkeypatch.setenv("GLT_RETRIGGER_GAP_US", "40000")
    config = worker_module._cleaning_config()
    assert config.min_confidence == 0.45
    assert config.min_duration_us == 120_000
    assert config.retrigger_gap_us == 40_000


def test_worker_reads_arrangement_options_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLT_ARRANGEMENT", "balanced")
    monkeypatch.setenv("GLT_ONSET_WINDOW_US", "175000")
    monkeypatch.setenv("GLT_MAX_VOICES", "3")
    config = worker_module._arrangement_config()
    assert config.enabled
    assert config.onset_window_us == 175_000
    assert config.max_voices == 3


def test_auto_cleaning_profile_preserves_candidates_for_arrangement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLT_CLEANING_PROFILE", "auto")
    config = worker_module._cleaning_config()
    assert config.min_confidence == 0.2
    assert config.min_duration_us == 50_000


def test_explicit_strict_profile_selects_strict_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLT_CLEANING_PROFILE", "strict")
    config = worker_module._cleaning_config()
    assert config.min_confidence == 0.5
    assert config.min_duration_us == 150_000
