"""JSONL worker service for one active transcription job at a time."""

from __future__ import annotations

import json
import logging
import pathlib
import sys
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from glt_core import __version__
from glt_core.media import (
    MediaError,
    build_extraction_plan,
    extract_audio,
    probe_media,
    resolve_ffmpeg_tools,
    sha256_file,
)
from glt_core.transcription import (
    BasicPitchRuntime,
    TranscriptionCancelled,
    TranscriptionError,
    load_basic_pitch_runtime,
    transcribe_to_midi,
)
from glt_core.transcription.onnx_probe import ModelResourceError

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 1024 * 1024
MODEL_VERSION = "basic-pitch-0.4.0/nmp.onnx"
SOURCE_MIDI_NAME = "source.mid"
REPORT_NAME = "report.json"
DECODED_AUDIO_NAME = "source.decoded.wav"


class ResponseWriter:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def send(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        job_id: str | None,
    ) -> None:
        document = {
            "protocol_version": PROTOCOL_VERSION,
            "job_id": job_id,
            "type": kind,
            "payload": payload,
        }
        line = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


class WorkerServer:
    def __init__(
        self,
        input_stream: Any,
        output_stream: Any,
        *,
        model_path: pathlib.Path | str | None = None,
    ) -> None:
        self._input = input_stream
        self._writer = ResponseWriter(output_stream)
        self._model_path = model_path
        self._runtime: BasicPitchRuntime | None = None
        self._lock = threading.Lock()
        self._active_job_id: str | None = None
        self._cancel_event: threading.Event | None = None
        self._job_thread: threading.Thread | None = None

    def run(self) -> int:
        try:
            self._runtime = load_basic_pitch_runtime(self._model_path)
        except ModelResourceError as exc:
            print(f"worker resource error: {exc}", file=sys.stderr)
            return 3
        self._writer.send(
            kind="ready",
            payload={
                "worker_version": __version__,
                "application_version": __version__,
                "model_version": MODEL_VERSION,
            },
            job_id=None,
        )
        while True:
            raw_line = self._input.buffer.readline(MAX_LINE_BYTES + 1)
            if not raw_line:
                break
            if len(raw_line) > MAX_LINE_BYTES:
                self._protocol_error(None, "LINE_TOO_LONG", "worker input line exceeds 1 MiB")
                return 1
            try:
                message = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._protocol_error(None, "INVALID_JSON", str(exc))
                continue
            self._handle_message(message)
        self._cancel_active()
        with self._lock:
            thread = self._job_thread
        if thread is not None:
            thread.join(timeout=5)
        return 0

    def _handle_message(self, message: Any) -> None:
        if not isinstance(message, dict):
            self._protocol_error(None, "SCHEMA_INVALID", "message must be an object")
            return
        version = message.get("protocol_version")
        job_id = message.get("job_id")
        kind = message.get("type")
        if version != PROTOCOL_VERSION:
            self._protocol_error(
                job_id if isinstance(job_id, str) else None,
                "UNSUPPORTED_VERSION",
                f"unsupported protocol version: {version!r}",
            )
            return
        if kind == "start":
            self._start_job(message)
        elif kind == "cancel":
            self._cancel_job(message)
        else:
            self._protocol_error(
                job_id if isinstance(job_id, str) else None,
                "UNKNOWN_MESSAGE",
                f"unsupported request type: {kind!r}",
            )

    def _start_job(self, message: dict[str, Any]) -> None:
        job_id = message.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            self._protocol_error(None, "INVALID_JOB_ID", "start job_id is required")
            return
        with self._lock:
            if self._job_thread is not None and self._job_thread.is_alive():
                self._writer.send(
                    kind="error",
                    job_id=job_id,
                    payload={
                        "code": "JOB_ACTIVE",
                        "message": "worker already has an active job",
                        "retryable": True,
                    },
                )
                return
            cancel_event = threading.Event()
            self._active_job_id = job_id
            self._cancel_event = cancel_event
            thread = threading.Thread(
                target=self._run_job,
                args=(message, cancel_event),
                name=f"glt-job-{job_id}",
                daemon=True,
            )
            self._job_thread = thread
            thread.start()

    def _cancel_job(self, message: dict[str, Any]) -> None:
        job_id = message.get("job_id")
        with self._lock:
            active = self._active_job_id
            cancel_event = self._cancel_event
        if not isinstance(job_id, str) or job_id != active or cancel_event is None:
            self._protocol_error(
                job_id if isinstance(job_id, str) else None,
                "JOB_MISMATCH",
                "cancel does not match the active job",
            )
            return
        cancel_event.set()

    def _cancel_active(self) -> None:
        with self._lock:
            event = self._cancel_event
        if event is not None:
            event.set()

    def _run_job(self, message: dict[str, Any], cancel_event: threading.Event) -> None:
        job_id = str(message["job_id"])
        try:
            payload = message.get("payload")
            if not isinstance(payload, dict):
                raise WorkerJobError("SCHEMA_INVALID", "start payload must be an object")
            operation = payload.get("operation")
            if operation != "transcribe":
                raise WorkerJobError(
                    "NOT_IMPLEMENTED",
                    f"operation is not implemented: {operation!r}",
                )
            result = self._transcribe(payload, job_id, cancel_event.is_set)
            self._writer.send(kind="result", job_id=job_id, payload=result)
        except (TranscriptionCancelled, MediaError, TranscriptionError) as exc:
            if getattr(exc, "code", None) == "CANCELLED":
                self._writer.send(kind="cancelled", job_id=job_id, payload={})
            else:
                self._writer.send(
                    kind="error",
                    job_id=job_id,
                    payload={
                        "code": str(getattr(exc, "code", "PROCESSING_FAILED")),
                        "message": str(exc),
                        "retryable": False,
                    },
                )
        except WorkerJobError as exc:
            self._writer.send(
                kind="error",
                job_id=job_id,
                payload={"code": exc.code, "message": str(exc), "retryable": False},
            )
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            traceback.print_exc(file=sys.stderr)
            self._writer.send(
                kind="error",
                job_id=job_id,
                payload={
                    "code": "INTERNAL_ERROR",
                    "message": f"unexpected worker failure: {exc}",
                    "retryable": False,
                },
            )
        finally:
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None
                    self._cancel_event = None

    def _transcribe(
        self,
        payload: dict[str, Any],
        job_id: str,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        input_path = pathlib.Path(str(payload.get("input_path", ""))).expanduser().resolve()
        staging_dir = pathlib.Path(str(payload.get("staging_dir", ""))).expanduser().resolve()
        if not input_path.is_file():
            raise WorkerJobError("INPUT_NOT_FOUND", "input file does not exist")
        input_hash = sha256_file(input_path)
        staging_dir.mkdir(parents=True, exist_ok=True)
        options = payload.get("options")
        if not isinstance(options, dict):
            raise WorkerJobError("SCHEMA_INVALID", "options must be an object")
        audio_track = options.get("audio_track")
        if audio_track is not None and (
            isinstance(audio_track, bool) or not isinstance(audio_track, int) or audio_track < 0
        ):
            raise WorkerJobError("INVALID_AUDIO_TRACK", "audio_track must be non-negative")
        start_us = _optional_integer(options.get("start_us"), "start_us")
        end_us = _optional_integer(options.get("end_us"), "end_us")

        self._writer.send(
            kind="progress",
            job_id=job_id,
            payload={"stage": "validating", "fraction": 0.0},
        )
        tools = resolve_ffmpeg_tools()
        self._writer.send(
            kind="progress",
            job_id=job_id,
            payload={"stage": "extracting", "fraction": 0.05},
        )
        media = probe_media(input_path, tools=tools, cancelled=cancelled)
        plan = build_extraction_plan(
            media,
            staging_dir / DECODED_AUDIO_NAME,
            audio_track=audio_track,
            start_us=start_us,
            end_us=end_us,
        )
        extracted = extract_audio(plan, tools=tools, overwrite=True, cancelled=cancelled)
        decoded_path = extracted
        try:

            def progress(stage: str, fraction: float) -> None:
                self._writer.send(
                    kind="progress",
                    job_id=job_id,
                    payload={"stage": stage, "fraction": 0.1 + 0.75 * fraction},
                )

            source_midi = staging_dir / SOURCE_MIDI_NAME
            transcription = transcribe_to_midi(
                decoded_path,
                source_midi,
                model_path=self._model_path,
                runtime=self._runtime,
                progress=progress,
                cancelled=cancelled,
            )
            source_hash = sha256_file(source_midi)
            report = {
                "schema_version": 1,
                "application_version": __version__,
                "engine": {"name": "basic-pitch", "version": "0.4.0", "backend": "onnxruntime-cpu"},
                "model": {
                    "name": "nmp",
                    "version": "0.4.0",
                    "sha256": sha256_file(pathlib.Path(transcription.model.path)),
                },
                "input": {
                    "source_type": _source_type(input_path),
                    "filename": input_path.name,
                    "sha256": input_hash,
                    "segment_start_us": start_us or 0,
                },
                "parameters": _report_parameters(options),
                "selected_track": plan.stream.position,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "counts": {
                    "input_notes": transcription.note_count,
                    "output_notes": transcription.note_count,
                    "dropped_notes": 0,
                    "mapped_keys": 0,
                    "replaced_semitones": 0,
                    "octave_folds": 0,
                    "duplicate_keys": 0,
                    "compatibility_collisions": 0,
                },
                "warnings": [],
                "artifacts": [
                    {
                        "kind": "source_midi",
                        "relative_path": SOURCE_MIDI_NAME,
                        "sha256": source_hash,
                        "size_bytes": source_midi.stat().st_size,
                    }
                ],
            }
            report_path = staging_dir / REPORT_NAME
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            artifacts = [
                {
                    "kind": "source_midi",
                    "relative_path": SOURCE_MIDI_NAME,
                    "sha256": source_hash,
                    "size_bytes": source_midi.stat().st_size,
                },
                {
                    "kind": "report",
                    "relative_path": REPORT_NAME,
                    "sha256": sha256_file(report_path),
                    "size_bytes": report_path.stat().st_size,
                },
            ]
            self._writer.send(
                kind="progress",
                job_id=job_id,
                payload={"stage": "completed", "fraction": 1.0},
            )
            return {
                "output_dir": str(staging_dir),
                "report_path": REPORT_NAME,
                "artifacts": artifacts,
            }
        finally:
            decoded_path.unlink(missing_ok=True)

    def _protocol_error(self, job_id: str | None, code: str, message: str) -> None:
        self._writer.send(
            kind="error",
            job_id=job_id,
            payload={"code": code, "message": message, "retryable": False},
        )


class WorkerJobError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _optional_integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkerJobError("INVALID_RANGE", f"{name} must be a non-negative integer")
    return value


def _source_type(path: pathlib.Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".mid", ".midi"}:
        return "midi"
    if suffix in {".mp4", ".mkv", ".mov", ".webm", ".avi"}:
        return "video"
    return "audio"


def _report_parameters(options: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("timing", "transpose", "audio_track", "start_us", "end_us", "preview_wav"):
        value = options.get(key)
        if value is not None:
            result[key] = value
    return result


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    return WorkerServer(sys.stdin, sys.stdout).run()


if __name__ == "__main__":
    raise SystemExit(main())
