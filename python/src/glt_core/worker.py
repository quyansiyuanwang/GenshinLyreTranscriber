"""JSONL worker service for one active transcription job at a time."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, cast

from glt_core import __version__
from glt_core.analysis_cli import main as analysis_main
from glt_core.cache import (
    CANDIDATE_CACHE_NAME,
    CandidateCache,
    read_candidate_cache,
    write_candidate_cache,
)
from glt_core.domain.midi_import import (
    MidiImportError,
    copy_source_midi,
    import_midi,
)
from glt_core.domain.note_sequence import NoteSequence
from glt_core.export import (
    CompatibilityScore,
    build_compatibility_score,
    build_events_document,
    build_readable_score,
    write_events_document,
    write_note_sequence_midi,
    write_text_score,
)
from glt_core.frozen_compat import install_frozen_replace_fallback
from glt_core.media import (
    MediaError,
    build_extraction_plan,
    extract_audio,
    probe_media,
    resolve_ffmpeg_tools,
    sha256_file,
)
from glt_core.performance import (
    PERFORMANCE_NAME,
    PerformanceBundle,
    PerformanceDocument,
    PerformanceRevision,
    build_performance,
    read_performance,
    render_performance_bundle,
)
from glt_core.processing import (
    ArrangementConfig,
    CleanConfig,
    FilterSpec,
    MappingConfig,
    QuantizationConfig,
    analyze_timing,
    apply_filter,
    arrange_note_sequence,
    clean_note_sequence,
    default_mapping_layout,
    filter_preset,
    filter_spec_from_dict,
    legacy_filter_spec,
    map_note_sequence,
    quantize_note_sequence,
)
from glt_core.protocol import validate_report
from glt_core.separation.cli import main as separation_main
from glt_core.separation.demucs_worker import main as demucs_main
from glt_core.synthesis import synthesize_preview_wav
from glt_core.transcription import (
    BasicPitchRuntime,
    TranscriptionCancelled,
    TranscriptionError,
    load_basic_pitch_runtime,
    transcribe_to_midi,
)
from glt_core.transcription.onnx_probe import ModelResourceError

install_frozen_replace_fallback()

PROTOCOL_VERSION = 3
MAX_LINE_BYTES = 1024 * 1024
MODEL_VERSION = "basic-pitch-0.4.0/nmp.onnx"
SOURCE_MIDI_NAME = "source.mid"
CLEANED_MIDI_NAME = "cleaned.mid"
MAPPED_MIDI_NAME = "mapped.mid"
EVENTS_NAME = "score.events.json"
READABLE_NAME = "score.readable.txt"
COMPAT_NAME = "score.compat.txt"
PREVIEW_NAME = "preview.wav"
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
        line = json.dumps(document, ensure_ascii=True, separators=(",", ":"))
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
            if operation == "transcribe":
                result = self._transcribe(payload, job_id, cancel_event.is_set)
            elif operation == "convert_midi":
                result = self._convert_midi(payload, job_id, cancel_event.is_set)
            elif operation == "refilter":
                result = self._refilter(payload, job_id, cancel_event.is_set)
            elif operation == "render_performance":
                result = self._render_performance(payload, job_id, cancel_event.is_set)
            elif operation == "edit_export":
                result = self._edit_export(payload, job_id, cancel_event.is_set)
            else:
                raise WorkerJobError(
                    "NOT_IMPLEMENTED",
                    f"operation is not implemented: {operation!r}",
                )
            self._writer.send(kind="result", job_id=job_id, payload=result)
        except (TranscriptionCancelled, MediaError, TranscriptionError, MidiImportError) as exc:
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
            cleaning_config = _cleaning_config()
            cleaning = clean_note_sequence(transcription.note_sequence, cleaning_config)
            timing = analyze_timing(decoded_path, cleaning.cleaned)
            if timing.fallback:
                self._writer.send(
                    kind="warning",
                    job_id=job_id,
                    payload={
                        "code": "TIMING_FALLBACK",
                        "message": (
                            "automatic timing was not used; original onsets were preserved "
                            f"({timing.reason})"
                        ),
                        "details": {
                            "confidence": timing.confidence,
                            "beat_count": timing.beat_count,
                        },
                    },
                )
            quantization = quantize_note_sequence(
                timing.sequence,
                _quantization_config(options, default_mode="auto"),
            )
            arrangement_config = _arrangement_config()
            arrangement = arrange_note_sequence(quantization.quantized, arrangement_config)
            mapping = map_note_sequence(
                arrangement.arranged,
                _mapping_config(options),
            )
            _performance_document, performance_bundle = _render_performance_for_mapping(
                staging_dir,
                mapping,
                source_type=_source_type(input_path),
                revision_source="transcribe",
                source_offset_us=start_us or 0,
                title=input_path.name,
                timing_mode=str(options.get("timing", "auto")),
                preview_wav=bool(options.get("preview_wav", False)),
            )
            performance_artifacts = _performance_artifacts(performance_bundle)
            selection_spec = legacy_filter_spec(
                min_confidence=cleaning_config.min_confidence,
                min_duration_us=cleaning_config.min_duration_us,
            )
            selection_matched = (
                cleaning.stats.input_notes
                - cleaning.stats.dropped_low_confidence
                - cleaning.stats.dropped_short
            )
            selection_dropped = cleaning.stats.dropped_low_confidence + cleaning.stats.dropped_short
            candidate_sequence = replace(
                transcription.note_sequence,
                tempo_map=timing.sequence.tempo_map,
                beat_grid=timing.sequence.beat_grid,
            )
            candidate_cache = CandidateCache(
                sequence=candidate_sequence,
                clean_config=cleaning_config,
                report_context={
                    "input": {
                        "source_type": _source_type(input_path),
                        "filename": input_path.name,
                        "sha256": input_hash,
                        "segment_start_us": start_us or 0,
                    },
                    "engine": {
                        "name": "basic-pitch",
                        "version": "0.4.0",
                        "backend": "onnxruntime-cpu",
                    },
                    "model": {
                        "name": "nmp",
                        "version": "0.4.0",
                        "sha256": sha256_file(pathlib.Path(transcription.model.path)),
                    },
                    "selected_track": plan.stream.position,
                    "arrangement": _arrangement_config_document(arrangement_config),
                    "source_midi": {
                        "sha256": source_hash,
                        "size_bytes": source_midi.stat().st_size,
                    },
                },
                original_parameters=_report_parameters(options),
                resolved_transpose=mapping.stats.transpose_semitones,
                mapping_profile=str(mapping.mapped.provenance.parameters["mapping"]["profile"]),
            )
            candidate_path = write_candidate_cache(
                candidate_cache,
                staging_dir / CANDIDATE_CACHE_NAME,
                overwrite=True,
            )
            candidate_hash = sha256_file(candidate_path)
            cleaned_midi = write_note_sequence_midi(
                quantization.quantized,
                staging_dir / CLEANED_MIDI_NAME,
                overwrite=True,
            )
            cleaned_hash = sha256_file(cleaned_midi)
            mapped_midi = write_note_sequence_midi(
                mapping.mapped,
                staging_dir / MAPPED_MIDI_NAME,
                overwrite=True,
            )
            mapped_hash = sha256_file(mapped_midi)
            mapping_profile = str(mapping.mapped.provenance.parameters["mapping"]["profile"])
            events_document = build_events_document(
                mapping.mapped,
                mapping.events,
                generator=f"GenshinLyreTranscriber {__version__}",
                mapping_profile=mapping_profile,
            )
            events_path = write_events_document(
                events_document,
                staging_dir / EVENTS_NAME,
                overwrite=True,
            )
            events_hash = sha256_file(events_path)
            text_exports = _write_text_scores(
                staging_dir,
                mapping.mapped,
                events_document,
                title=input_path.name,
                timing_mode=str(options.get("timing", "auto")),
                transpose_semitones=mapping.stats.transpose_semitones,
                mapping_profile=mapping_profile,
                source_offset_us=start_us or 0,
            )
            preview_requested = bool(options.get("preview_wav", False))
            preview = _write_preview(staging_dir, events_document, preview_requested)
            preview_artifacts = _preview_artifact_entries(preview)
            cleaning_removed = (
                cleaning.stats.dropped_low_confidence
                + cleaning.stats.dropped_short
                + cleaning.stats.duplicate_notes_removed
                + cleaning.stats.overlapped_notes_merged
            )
            report = {
                "schema_version": 3,
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
                "parameters": {
                    **_report_parameters(options),
                    "filter": selection_spec.to_dict(),
                },
                "selected_track": plan.stream.position,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "counts": {
                    "input_notes": cleaning.stats.input_notes,
                    "output_notes": mapping.stats.mapped_notes,
                    "dropped_notes": (
                        cleaning_removed
                        + arrangement.stats.dropped_notes
                        + mapping.stats.collision_notes_removed
                    ),
                    "mapped_keys": mapping.stats.unique_keys_used,
                    "replaced_semitones": mapping.stats.replaced_semitones,
                    "octave_folds": mapping.stats.octave_folds,
                    "duplicate_keys": mapping.stats.collision_notes_removed,
                    "compatibility_collisions": text_exports.compatibility.collisions,
                },
                "selection": {
                    "format_version": 1,
                    "source": "legacy_cleaning",
                    "spec": selection_spec.to_dict(),
                    "matched_notes": selection_matched,
                    "dropped_notes": selection_dropped,
                    "rule_hits": [selection_matched],
                },
                "warnings": _report_warnings(
                    cleaning_removed,
                    timing.fallback,
                    len(quantization.fallback_regions),
                    cleaning_config,
                    arrangement_config,
                    arrangement.stats.dropped_notes,
                )
                + _compatibility_warnings(text_exports.compatibility)
                + _preview_warnings(
                    preview_requested,
                    preview,
                    events_document,
                ),
                "artifacts": [
                    {
                        "kind": "candidate_cache",
                        "relative_path": CANDIDATE_CACHE_NAME,
                        "sha256": candidate_hash,
                        "size_bytes": candidate_path.stat().st_size,
                    },
                    *performance_artifacts,
                    {
                        "kind": "source_midi",
                        "relative_path": SOURCE_MIDI_NAME,
                        "sha256": source_hash,
                        "size_bytes": source_midi.stat().st_size,
                    },
                    {
                        "kind": "cleaned_midi",
                        "relative_path": CLEANED_MIDI_NAME,
                        "sha256": cleaned_hash,
                        "size_bytes": cleaned_midi.stat().st_size,
                    },
                    {
                        "kind": "mapped_midi",
                        "relative_path": MAPPED_MIDI_NAME,
                        "sha256": mapped_hash,
                        "size_bytes": mapped_midi.stat().st_size,
                    },
                    {
                        "kind": "events",
                        "relative_path": EVENTS_NAME,
                        "sha256": events_hash,
                        "size_bytes": events_path.stat().st_size,
                    },
                    {
                        "kind": "readable_text",
                        "relative_path": READABLE_NAME,
                        "sha256": text_exports.readable_hash,
                        "size_bytes": text_exports.readable_path.stat().st_size,
                    },
                    {
                        "kind": "compat_text",
                        "relative_path": COMPAT_NAME,
                        "sha256": text_exports.compatibility_hash,
                        "size_bytes": text_exports.compatibility_path.stat().st_size,
                    },
                    *preview_artifacts,
                ],
            }
            validate_report(report)
            report_path = staging_dir / REPORT_NAME
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            artifacts = [
                {
                    "kind": "candidate_cache",
                    "relative_path": CANDIDATE_CACHE_NAME,
                    "sha256": candidate_hash,
                    "size_bytes": candidate_path.stat().st_size,
                },
                *performance_artifacts,
                {
                    "kind": "source_midi",
                    "relative_path": SOURCE_MIDI_NAME,
                    "sha256": source_hash,
                    "size_bytes": source_midi.stat().st_size,
                },
                {
                    "kind": "cleaned_midi",
                    "relative_path": CLEANED_MIDI_NAME,
                    "sha256": cleaned_hash,
                    "size_bytes": cleaned_midi.stat().st_size,
                },
                {
                    "kind": "mapped_midi",
                    "relative_path": MAPPED_MIDI_NAME,
                    "sha256": mapped_hash,
                    "size_bytes": mapped_midi.stat().st_size,
                },
                {
                    "kind": "events",
                    "relative_path": EVENTS_NAME,
                    "sha256": events_hash,
                    "size_bytes": events_path.stat().st_size,
                },
                {
                    "kind": "readable_text",
                    "relative_path": READABLE_NAME,
                    "sha256": text_exports.readable_hash,
                    "size_bytes": text_exports.readable_path.stat().st_size,
                },
                {
                    "kind": "compat_text",
                    "relative_path": COMPAT_NAME,
                    "sha256": text_exports.compatibility_hash,
                    "size_bytes": text_exports.compatibility_path.stat().st_size,
                },
                *preview_artifacts,
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

    def _convert_midi(
        self,
        payload: dict[str, Any],
        job_id: str,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        input_path = pathlib.Path(str(payload.get("input_path", ""))).expanduser().resolve()
        staging_dir = pathlib.Path(str(payload.get("staging_dir", ""))).expanduser().resolve()
        if not input_path.is_file():
            raise WorkerJobError("INPUT_NOT_FOUND", "input MIDI file does not exist")
        staging_dir.mkdir(parents=True, exist_ok=True)
        options = payload.get("options")
        if not isinstance(options, dict):
            raise WorkerJobError("SCHEMA_INVALID", "options must be an object")
        self._writer.send(
            kind="progress",
            job_id=job_id,
            payload={"stage": "validating", "fraction": 0.0},
        )
        _raise_if_cancelled(cancelled)
        imported = import_midi(input_path)
        _raise_if_cancelled(cancelled)
        cleaning_config = _cleaning_config()
        cleaning = clean_note_sequence(imported.sequence, cleaning_config)
        timing = analyze_timing(None, cleaning.cleaned)
        quantization = quantize_note_sequence(
            timing.sequence,
            _quantization_config(options, default_mode="preserve"),
        )
        arrangement_config = _arrangement_config()
        arrangement = arrange_note_sequence(quantization.quantized, arrangement_config)
        mapping = map_note_sequence(arrangement.arranged, _mapping_config(options))
        _performance_document, performance_bundle = _render_performance_for_mapping(
            staging_dir,
            mapping,
            source_type="midi",
            revision_source="convert_midi",
            source_offset_us=0,
            title=input_path.name,
            timing_mode=str(options.get("timing", "preserve")),
            preview_wav=bool(options.get("preview_wav", False)),
        )
        performance_artifacts = _performance_artifacts(performance_bundle)
        selection_spec = legacy_filter_spec(
            min_confidence=cleaning_config.min_confidence,
            min_duration_us=cleaning_config.min_duration_us,
        )
        selection_matched = (
            cleaning.stats.input_notes
            - cleaning.stats.dropped_low_confidence
            - cleaning.stats.dropped_short
        )
        selection_dropped = cleaning.stats.dropped_low_confidence + cleaning.stats.dropped_short
        cleaned_midi = write_note_sequence_midi(
            quantization.quantized,
            staging_dir / CLEANED_MIDI_NAME,
            overwrite=True,
        )
        cleaned_hash = sha256_file(cleaned_midi)
        mapped_midi = write_note_sequence_midi(
            mapping.mapped,
            staging_dir / MAPPED_MIDI_NAME,
            overwrite=True,
        )
        mapped_hash = sha256_file(mapped_midi)
        mapping_profile = str(mapping.mapped.provenance.parameters["mapping"]["profile"])
        events_document = build_events_document(
            mapping.mapped,
            mapping.events,
            generator=f"GenshinLyreTranscriber {__version__}",
            mapping_profile=mapping_profile,
        )
        events_path = write_events_document(
            events_document,
            staging_dir / EVENTS_NAME,
            overwrite=True,
        )
        events_hash = sha256_file(events_path)
        text_exports = _write_text_scores(
            staging_dir,
            mapping.mapped,
            events_document,
            title=input_path.name,
            timing_mode=str(options.get("timing", "preserve")),
            transpose_semitones=mapping.stats.transpose_semitones,
            mapping_profile=mapping_profile,
            source_offset_us=0,
        )
        preview_requested = bool(options.get("preview_wav", False))
        preview = _write_preview(staging_dir, events_document, preview_requested)
        preview_artifacts = _preview_artifact_entries(preview)
        cleaning_removed = (
            cleaning.stats.dropped_low_confidence
            + cleaning.stats.dropped_short
            + cleaning.stats.duplicate_notes_removed
            + cleaning.stats.overlapped_notes_merged
        )
        for warning in imported.warnings:
            self._writer.send(
                kind="warning",
                job_id=job_id,
                payload={
                    "code": warning.code,
                    "message": warning.message,
                    "details": {"count": warning.count},
                },
            )
        source_midi = staging_dir / SOURCE_MIDI_NAME
        copy_source_midi(input_path, source_midi, overwrite=True)
        source_hash = sha256_file(source_midi)
        candidate_cache = CandidateCache(
            sequence=replace(
                imported.sequence,
                tempo_map=timing.sequence.tempo_map,
                beat_grid=timing.sequence.beat_grid,
            ),
            clean_config=cleaning_config,
            report_context={
                "input": {
                    "source_type": "midi",
                    "filename": input_path.name,
                    "sha256": imported.source_sha256,
                    "segment_start_us": 0,
                },
                "engine": {"name": "midi-import", "version": "mido", "backend": "python"},
                "model": None,
                "selected_track": None,
                "arrangement": _arrangement_config_document(arrangement_config),
                "source_midi": {
                    "sha256": source_hash,
                    "size_bytes": source_midi.stat().st_size,
                },
            },
            original_parameters=_report_parameters(options),
            resolved_transpose=mapping.stats.transpose_semitones,
            mapping_profile=mapping_profile,
        )
        candidate_path = write_candidate_cache(
            candidate_cache,
            staging_dir / CANDIDATE_CACHE_NAME,
            overwrite=True,
        )
        candidate_hash = sha256_file(candidate_path)
        report = {
            "schema_version": 3,
            "application_version": __version__,
            "engine": {"name": "midi-import", "version": "mido", "backend": "python"},
            "model": None,
            "input": {
                "source_type": "midi",
                "filename": input_path.name,
                "sha256": imported.source_sha256,
                "segment_start_us": 0,
            },
            "parameters": {
                **_report_parameters(options),
                "filter": selection_spec.to_dict(),
            },
            "selected_track": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "counts": {
                "input_notes": cleaning.stats.input_notes,
                "output_notes": mapping.stats.mapped_notes,
                "dropped_notes": (
                    cleaning_removed
                    + arrangement.stats.dropped_notes
                    + mapping.stats.collision_notes_removed
                ),
                "mapped_keys": mapping.stats.unique_keys_used,
                "replaced_semitones": mapping.stats.replaced_semitones,
                "octave_folds": mapping.stats.octave_folds,
                "duplicate_keys": mapping.stats.collision_notes_removed,
                "compatibility_collisions": text_exports.compatibility.collisions,
            },
            "selection": {
                "format_version": 1,
                "source": "legacy_cleaning",
                "spec": selection_spec.to_dict(),
                "matched_notes": selection_matched,
                "dropped_notes": selection_dropped,
                "rule_hits": [selection_matched],
            },
            "warnings": [
                {"code": warning.code, "message": warning.message} for warning in imported.warnings
            ]
            + (
                _report_warnings(
                    cleaning_removed,
                    timing.fallback,
                    len(quantization.fallback_regions),
                    cleaning_config,
                    arrangement_config,
                    arrangement.stats.dropped_notes,
                )
            )
            + _compatibility_warnings(text_exports.compatibility)
            + _preview_warnings(
                preview_requested,
                preview,
                events_document,
            ),
            "artifacts": [
                {
                    "kind": "candidate_cache",
                    "relative_path": CANDIDATE_CACHE_NAME,
                    "sha256": candidate_hash,
                    "size_bytes": candidate_path.stat().st_size,
                },
                *performance_artifacts,
                {
                    "kind": "source_midi",
                    "relative_path": SOURCE_MIDI_NAME,
                    "sha256": source_hash,
                    "size_bytes": source_midi.stat().st_size,
                },
                {
                    "kind": "cleaned_midi",
                    "relative_path": CLEANED_MIDI_NAME,
                    "sha256": cleaned_hash,
                    "size_bytes": cleaned_midi.stat().st_size,
                },
                {
                    "kind": "mapped_midi",
                    "relative_path": MAPPED_MIDI_NAME,
                    "sha256": mapped_hash,
                    "size_bytes": mapped_midi.stat().st_size,
                },
                {
                    "kind": "events",
                    "relative_path": EVENTS_NAME,
                    "sha256": events_hash,
                    "size_bytes": events_path.stat().st_size,
                },
                {
                    "kind": "readable_text",
                    "relative_path": READABLE_NAME,
                    "sha256": text_exports.readable_hash,
                    "size_bytes": text_exports.readable_path.stat().st_size,
                },
                {
                    "kind": "compat_text",
                    "relative_path": COMPAT_NAME,
                    "sha256": text_exports.compatibility_hash,
                    "size_bytes": text_exports.compatibility_path.stat().st_size,
                },
                *preview_artifacts,
            ],
        }
        validate_report(report)
        report_path = staging_dir / REPORT_NAME
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        artifacts = [
            {
                "kind": "candidate_cache",
                "relative_path": CANDIDATE_CACHE_NAME,
                "sha256": candidate_hash,
                "size_bytes": candidate_path.stat().st_size,
            },
            *performance_artifacts,
            {
                "kind": "source_midi",
                "relative_path": SOURCE_MIDI_NAME,
                "sha256": source_hash,
                "size_bytes": source_midi.stat().st_size,
            },
            {
                "kind": "cleaned_midi",
                "relative_path": CLEANED_MIDI_NAME,
                "sha256": cleaned_hash,
                "size_bytes": cleaned_midi.stat().st_size,
            },
            {
                "kind": "mapped_midi",
                "relative_path": MAPPED_MIDI_NAME,
                "sha256": mapped_hash,
                "size_bytes": mapped_midi.stat().st_size,
            },
            {
                "kind": "events",
                "relative_path": EVENTS_NAME,
                "sha256": events_hash,
                "size_bytes": events_path.stat().st_size,
            },
            {
                "kind": "readable_text",
                "relative_path": READABLE_NAME,
                "sha256": text_exports.readable_hash,
                "size_bytes": text_exports.readable_path.stat().st_size,
            },
            {
                "kind": "compat_text",
                "relative_path": COMPAT_NAME,
                "sha256": text_exports.compatibility_hash,
                "size_bytes": text_exports.compatibility_path.stat().st_size,
            },
            *preview_artifacts,
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

    def _refilter(
        self,
        payload: dict[str, Any],
        job_id: str,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        source_dir = pathlib.Path(str(payload.get("input_path", ""))).expanduser().resolve()
        staging_dir = pathlib.Path(str(payload.get("staging_dir", ""))).expanduser().resolve()
        if not source_dir.is_dir():
            raise WorkerJobError("INPUT_NOT_FOUND", "source result directory does not exist")
        options = payload.get("options")
        if not isinstance(options, dict):
            raise WorkerJobError("SCHEMA_INVALID", "options must be an object")
        try:
            cache = read_candidate_cache(source_dir / CANDIDATE_CACHE_NAME)
        except (OSError, ValueError) as exc:
            raise WorkerJobError("CANDIDATE_CACHE_MISSING", str(exc)) from exc
        staging_dir.mkdir(parents=True, exist_ok=True)
        self._writer.send(
            kind="progress",
            job_id=job_id,
            payload={"stage": "validating", "fraction": 0.0},
        )
        _raise_if_cancelled(cancelled)
        try:
            filter_spec, automatic_diagnostics = _filter_selection(
                options,
                cache_sequence=cache.sequence,
            )
        except ValueError as exc:
            raise WorkerJobError("INVALID_FILTER", str(exc)) from exc
        filtered = apply_filter(cache.sequence, filter_spec)
        self._writer.send(
            kind="progress",
            job_id=job_id,
            payload={"stage": "filtering", "fraction": 0.15},
        )
        _raise_if_cancelled(cancelled)
        cleaning_config = CleanConfig(
            min_confidence=0.0,
            min_duration_us=0,
            retrigger_gap_us=cache.clean_config.retrigger_gap_us,
        )
        cleaning = clean_note_sequence(filtered.filtered, cleaning_config)
        quantization = quantize_note_sequence(
            cleaning.cleaned,
            _quantization_config(cache.original_parameters, default_mode="auto"),
        )
        arrangement_config = _arrangement_config_from_document(
            cache.report_context.get("arrangement")
        )
        arrangement = arrange_note_sequence(quantization.quantized, arrangement_config)
        mapping = map_note_sequence(
            arrangement.arranged,
            _mapping_config(cache.original_parameters),
        )
        context_input = cache.report_context.get("input")
        if not isinstance(context_input, dict):
            raise WorkerJobError("CACHE_INVALID", "candidate input metadata is invalid")
        performance_title = str(context_input.get("filename", "transcription"))
        performance_source_type = str(context_input.get("source_type", "audio"))
        performance_offset = context_input.get("segment_start_us", 0)
        if isinstance(performance_offset, bool) or not isinstance(performance_offset, int):
            performance_offset = 0
        _performance_document, performance_bundle = _render_performance_for_mapping(
            staging_dir,
            mapping,
            source_type=performance_source_type,
            revision_source="refilter",
            source_offset_us=max(0, performance_offset),
            title=performance_title,
            timing_mode=str(cache.original_parameters.get("timing", "auto")),
            preview_wav=bool(options.get("preview_wav", False)),
        )
        performance_artifacts = _performance_artifacts(performance_bundle)
        source_midi = staging_dir / SOURCE_MIDI_NAME
        source_source = source_dir / SOURCE_MIDI_NAME
        if not source_source.is_file():
            raise WorkerJobError("SOURCE_MIDI_MISSING", "source.mid is missing from result")
        shutil.copyfile(source_source, source_midi)
        source_hash = sha256_file(source_midi)
        cleaned_midi = write_note_sequence_midi(
            quantization.quantized,
            staging_dir / CLEANED_MIDI_NAME,
            overwrite=True,
        )
        cleaned_hash = sha256_file(cleaned_midi)
        mapped_midi = write_note_sequence_midi(
            mapping.mapped,
            staging_dir / MAPPED_MIDI_NAME,
            overwrite=True,
        )
        mapped_hash = sha256_file(mapped_midi)
        mapping_profile = str(mapping.mapped.provenance.parameters["mapping"]["profile"])
        events_document = build_events_document(
            mapping.mapped,
            mapping.events,
            generator=f"GenshinLyreTranscriber {__version__}",
            mapping_profile=mapping_profile,
        )
        events_path = write_events_document(
            events_document,
            staging_dir / EVENTS_NAME,
            overwrite=True,
        )
        events_hash = sha256_file(events_path)
        context_input = cache.report_context.get("input")
        if not isinstance(context_input, dict):
            raise WorkerJobError("CACHE_INVALID", "candidate input metadata is invalid")
        title = str(context_input.get("filename", "transcription"))
        timing_mode = str(cache.original_parameters.get("timing", "auto"))
        text_exports = _write_text_scores(
            staging_dir,
            mapping.mapped,
            events_document,
            title=title,
            timing_mode=timing_mode,
            transpose_semitones=mapping.stats.transpose_semitones,
            mapping_profile=mapping_profile,
            source_offset_us=cache.sequence.provenance.source_offset_us,
        )
        preview_value = options.get("preview_wav")
        if preview_value is None:
            preview_value = cache.original_parameters.get("preview_wav", False)
        preview_requested = bool(preview_value)
        preview = _write_preview(staging_dir, events_document, preview_requested)
        preview_artifacts = _preview_artifact_entries(preview)
        candidate_cache = CandidateCache(
            sequence=cache.sequence,
            clean_config=cache.clean_config,
            report_context=cache.report_context,
            original_parameters=cache.original_parameters,
            resolved_transpose=mapping.stats.transpose_semitones,
            mapping_profile=mapping_profile,
        )
        candidate_path = write_candidate_cache(
            candidate_cache,
            staging_dir / CANDIDATE_CACHE_NAME,
            overwrite=True,
        )
        candidate_hash = sha256_file(candidate_path)
        cleaning_removed = (
            cleaning.stats.dropped_low_confidence
            + cleaning.stats.dropped_short
            + cleaning.stats.duplicate_notes_removed
            + cleaning.stats.overlapped_notes_merged
        )
        timing_parameters = cache.sequence.provenance.parameters.get("timing")
        timing_fallback = bool(
            timing_parameters.get("fallback", False)
            if isinstance(timing_parameters, dict)
            else False
        )
        report = {
            "schema_version": 3,
            "application_version": __version__,
            "engine": cache.report_context.get("engine"),
            "model": cache.report_context.get("model"),
            "input": context_input,
            "parameters": {
                **cache.original_parameters,
                "filter": filter_spec.to_dict(),
            },
            "selected_track": cache.report_context.get("selected_track"),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "counts": {
                "input_notes": len(cache.sequence.notes),
                "output_notes": mapping.stats.mapped_notes,
                "dropped_notes": (
                    filtered.stats.dropped_notes
                    + cleaning_removed
                    + arrangement.stats.dropped_notes
                    + mapping.stats.collision_notes_removed
                ),
                "mapped_keys": mapping.stats.unique_keys_used,
                "replaced_semitones": mapping.stats.replaced_semitones,
                "octave_folds": mapping.stats.octave_folds,
                "duplicate_keys": mapping.stats.collision_notes_removed,
                "compatibility_collisions": text_exports.compatibility.collisions,
            },
            "selection": {
                "format_version": 1,
                "source": "refilter",
                "spec": filter_spec.to_dict(),
                "matched_notes": filtered.stats.matched_notes,
                "dropped_notes": filtered.stats.dropped_notes,
                "rule_hits": list(filtered.stats.rule_hits),
            },
            "warnings": _refilter_warnings(
                filtered.stats.dropped_notes,
                timing_fallback,
                len(quantization.fallback_regions),
                arrangement_config,
                arrangement.stats.dropped_notes,
                automatic_diagnostics,
            )
            + _compatibility_warnings(text_exports.compatibility)
            + _preview_warnings(preview_requested, preview, events_document),
            "artifacts": [
                {
                    "kind": "candidate_cache",
                    "relative_path": CANDIDATE_CACHE_NAME,
                    "sha256": candidate_hash,
                    "size_bytes": candidate_path.stat().st_size,
                },
                *performance_artifacts,
                {
                    "kind": "source_midi",
                    "relative_path": SOURCE_MIDI_NAME,
                    "sha256": source_hash,
                    "size_bytes": source_midi.stat().st_size,
                },
                {
                    "kind": "cleaned_midi",
                    "relative_path": CLEANED_MIDI_NAME,
                    "sha256": cleaned_hash,
                    "size_bytes": cleaned_midi.stat().st_size,
                },
                {
                    "kind": "mapped_midi",
                    "relative_path": MAPPED_MIDI_NAME,
                    "sha256": mapped_hash,
                    "size_bytes": mapped_midi.stat().st_size,
                },
                {
                    "kind": "events",
                    "relative_path": EVENTS_NAME,
                    "sha256": events_hash,
                    "size_bytes": events_path.stat().st_size,
                },
                {
                    "kind": "readable_text",
                    "relative_path": READABLE_NAME,
                    "sha256": text_exports.readable_hash,
                    "size_bytes": text_exports.readable_path.stat().st_size,
                },
                {
                    "kind": "compat_text",
                    "relative_path": COMPAT_NAME,
                    "sha256": text_exports.compatibility_hash,
                    "size_bytes": text_exports.compatibility_path.stat().st_size,
                },
                *preview_artifacts,
            ],
        }
        validate_report(report)
        report_path = staging_dir / REPORT_NAME
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        report_artifacts = report["artifacts"]
        assert isinstance(report_artifacts, list)
        artifacts = [
            *report_artifacts,
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

    def _render_performance(
        self,
        payload: dict[str, Any],
        job_id: str,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        source_input = pathlib.Path(str(payload.get("input_path", ""))).expanduser().resolve()
        staging_dir = pathlib.Path(str(payload.get("staging_dir", ""))).expanduser().resolve()
        if source_input.is_file() and source_input.name == PERFORMANCE_NAME:
            source_dir = source_input.parent
            performance_path = source_input
        elif source_input.is_dir():
            source_dir = source_input
            performance_path = source_dir / PERFORMANCE_NAME
        else:
            raise WorkerJobError("INPUT_NOT_FOUND", "performance result directory does not exist")
        if not performance_path.is_file():
            raise WorkerJobError(
                "PERFORMANCE_MISSING",
                "result has no performance.json; legacy results remain read-only",
            )
        options = payload.get("options")
        if not isinstance(options, dict):
            raise WorkerJobError("SCHEMA_INVALID", "options must be an object")
        staging_dir.mkdir(parents=True, exist_ok=True)
        self._writer.send(
            kind="progress",
            job_id=job_id,
            payload={"stage": "validating", "fraction": 0.0},
        )
        _raise_if_cancelled(cancelled)
        document = read_performance(performance_path)
        previous_report = _read_optional_report(source_dir)
        title_value = options.get("title")
        if title_value is not None and (
            not isinstance(title_value, str) or not title_value.strip()
        ):
            raise WorkerJobError("SCHEMA_INVALID", "performance title must be a non-empty string")
        title = (
            str(title_value).strip()
            if title_value is not None
            else _report_input_filename(previous_report) or "performance"
        )
        self._writer.send(
            kind="progress",
            job_id=job_id,
            payload={"stage": "exporting", "fraction": 0.25},
        )
        bundle = render_performance_bundle(
            document,
            staging_dir,
            title=title,
            timing_mode=str(options.get("timing", "preserve")),
            preview_wav=bool(options.get("preview_wav", False)),
            generator=f"GenshinLyreTranscriber {__version__}",
        )
        _raise_if_cancelled(cancelled)
        artifacts = list(bundle.artifacts)
        source_midi = staging_dir / SOURCE_MIDI_NAME
        source_candidate = staging_dir / CANDIDATE_CACHE_NAME
        original_source_midi = source_dir / SOURCE_MIDI_NAME
        original_candidate = source_dir / CANDIDATE_CACHE_NAME
        if original_source_midi.is_file():
            shutil.copyfile(original_source_midi, source_midi)
            artifacts.append(_artifact_entry("source_midi", source_midi))
        if original_candidate.is_file():
            shutil.copyfile(original_candidate, source_candidate)
            artifacts.append(_artifact_entry("candidate_cache", source_candidate))

        input_document = _performance_report_input(
            previous_report,
            performance_path=performance_path,
            document=document,
            fallback_title=title,
        )
        parameters = previous_report.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        parameters = {
            **parameters,
            "performance": {
                "format_version": document.format_version,
                "revision_id": document.revision.id,
                "mapping_profile": document.mapping.profile,
                "transpose_semitones": document.mapping.transpose_semitones,
            },
            "preview_wav": bool(options.get("preview_wav", False)),
        }
        engine = previous_report.get("engine")
        if not isinstance(engine, dict):
            engine = {"name": "performance-renderer", "version": "1", "backend": "python"}
        model = previous_report.get("model")
        if model is not None and not isinstance(model, dict):
            model = None
        selected_track = previous_report.get("selected_track")
        if isinstance(selected_track, bool) or not isinstance(selected_track, int):
            selected_track = None
        warnings = _compatibility_warnings(bundle.compatibility)
        if bool(options.get("preview_wav", False)) and not bundle.events_document["events"]:
            warnings.append(
                {
                    "code": "EMPTY_PREVIEW",
                    "message": "empty performance has no audible preview",
                }
            )
        report = {
            "schema_version": 3,
            "application_version": __version__,
            "engine": engine,
            "model": model,
            "input": input_document,
            "parameters": parameters,
            "selected_track": selected_track,
            "elapsed_ms": 0.0,
            "counts": {
                "input_notes": len(document.notes),
                "output_notes": len(document.notes),
                "dropped_notes": 0,
                "mapped_keys": len({note.key for note in document.notes}),
                "replaced_semitones": 0,
                "octave_folds": 0,
                "duplicate_keys": 0,
                "compatibility_collisions": bundle.compatibility.collisions,
            },
            "selection": {
                "format_version": 1,
                "source": "performance",
                "spec": {"format_version": 1, "rules": []},
                "matched_notes": len(document.notes),
                "dropped_notes": 0,
                "rule_hits": [len(document.notes)],
            },
            "warnings": warnings,
            "artifacts": artifacts,
        }
        validate_report(report)
        report_path = staging_dir / REPORT_NAME
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        artifacts.append(_artifact_entry("report", report_path))
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

    def _edit_export(
        self,
        payload: dict[str, Any],
        job_id: str,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        input_path = pathlib.Path(str(payload.get("input_path", ""))).expanduser().resolve()
        staging_dir = pathlib.Path(str(payload.get("staging_dir", ""))).expanduser().resolve()
        if not input_path.is_file():
            raise WorkerJobError("INPUT_NOT_FOUND", "edited performance file does not exist")
        options = payload.get("options")
        if not isinstance(options, dict):
            raise WorkerJobError("SCHEMA_INVALID", "options must be an object")
        source_value = options.get("source_result_dir")
        source_dir = input_path.parent
        if source_value is not None:
            source_dir = pathlib.Path(str(source_value)).expanduser().resolve()
            if not source_dir.is_dir():
                raise WorkerJobError(
                    "INPUT_NOT_FOUND",
                    "source performance result directory does not exist",
                )
        revision_id = options.get("revision_id", "edit-01")
        parent_revision_id = options.get("parent_revision_id")
        if not isinstance(revision_id, str) or not revision_id.strip() or len(revision_id) > 128:
            raise WorkerJobError("SCHEMA_INVALID", "edit revision_id is invalid")
        if parent_revision_id is not None and (
            not isinstance(parent_revision_id, str)
            or not parent_revision_id.strip()
            or len(parent_revision_id) > 128
        ):
            raise WorkerJobError("SCHEMA_INVALID", "parent_revision_id is invalid")
        staging_dir.mkdir(parents=True, exist_ok=True)
        self._writer.send(
            kind="progress",
            job_id=job_id,
            payload={"stage": "validating", "fraction": 0.0},
        )
        _raise_if_cancelled(cancelled)
        document = read_performance(input_path)
        document = replace(
            document,
            revision=PerformanceRevision(
                id=revision_id.strip(),
                parent_id=(
                    parent_revision_id.strip()
                    if isinstance(parent_revision_id, str)
                    else document.revision.id
                ),
                source="edit",
            ),
        )
        previous_report = _read_optional_report(source_dir)
        title_value = options.get("title")
        if title_value is not None and (
            not isinstance(title_value, str) or not title_value.strip()
        ):
            raise WorkerJobError("SCHEMA_INVALID", "edit title must be a non-empty string")
        title = (
            str(title_value).strip()
            if title_value is not None
            else _report_input_filename(previous_report) or "edited-performance"
        )
        self._writer.send(
            kind="progress",
            job_id=job_id,
            payload={"stage": "exporting", "fraction": 0.25},
        )
        bundle = render_performance_bundle(
            document,
            staging_dir,
            title=title,
            timing_mode=str(options.get("timing", "preserve")),
            preview_wav=bool(options.get("preview_wav", False)),
            generator=f"GenshinLyreTranscriber {__version__}",
        )
        _raise_if_cancelled(cancelled)
        artifacts = list(bundle.artifacts)
        source_midi = staging_dir / SOURCE_MIDI_NAME
        source_candidate = staging_dir / CANDIDATE_CACHE_NAME
        original_source_midi = source_dir / SOURCE_MIDI_NAME
        original_candidate = source_dir / CANDIDATE_CACHE_NAME
        if original_source_midi.is_file():
            shutil.copyfile(original_source_midi, source_midi)
            artifacts.append(_artifact_entry("source_midi", source_midi))
        if original_candidate.is_file():
            shutil.copyfile(original_candidate, source_candidate)
            artifacts.append(_artifact_entry("candidate_cache", source_candidate))

        input_document = _performance_report_input(
            previous_report,
            performance_path=input_path,
            document=document,
            fallback_title=title,
        )
        parameters = previous_report.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        parameters = {
            **parameters,
            "performance": {
                "format_version": document.format_version,
                "revision_id": document.revision.id,
                "mapping_profile": document.mapping.profile,
                "transpose_semitones": document.mapping.transpose_semitones,
            },
            "preview_wav": bool(options.get("preview_wav", False)),
        }
        engine = previous_report.get("engine")
        if not isinstance(engine, dict):
            engine = {"name": "performance-editor", "version": "1", "backend": "python"}
        model = previous_report.get("model")
        if model is not None and not isinstance(model, dict):
            model = None
        selected_track = previous_report.get("selected_track")
        if isinstance(selected_track, bool) or not isinstance(selected_track, int):
            selected_track = None
        warnings = _compatibility_warnings(bundle.compatibility)
        if bool(options.get("preview_wav", False)) and not bundle.events_document["events"]:
            warnings.append(
                {
                    "code": "EMPTY_PREVIEW",
                    "message": "empty performance has no audible preview",
                }
            )
        report = {
            "schema_version": 3,
            "application_version": __version__,
            "engine": engine,
            "model": model,
            "input": input_document,
            "parameters": parameters,
            "selected_track": selected_track,
            "elapsed_ms": 0.0,
            "counts": {
                "input_notes": len(document.notes),
                "output_notes": len(document.notes),
                "dropped_notes": 0,
                "mapped_keys": len({note.key for note in document.notes}),
                "replaced_semitones": 0,
                "octave_folds": 0,
                "duplicate_keys": 0,
                "compatibility_collisions": bundle.compatibility.collisions,
            },
            "selection": {
                "format_version": 1,
                "source": "edit",
                "spec": {"format_version": 1, "rules": []},
                "matched_notes": len(document.notes),
                "dropped_notes": 0,
                "rule_hits": [len(document.notes)],
            },
            "warnings": warnings,
            "artifacts": artifacts,
        }
        validate_report(report)
        report_path = staging_dir / REPORT_NAME
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        artifacts.append(_artifact_entry("report", report_path))
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

    def _protocol_error(self, job_id: str | None, code: str, message: str) -> None:
        self._writer.send(
            kind="error",
            job_id=job_id,
            payload={"code": code, "message": message, "retryable": False},
        )


@dataclass(frozen=True, slots=True)
class _TextExports:
    readable_path: pathlib.Path
    readable_hash: str
    compatibility_path: pathlib.Path
    compatibility_hash: str
    compatibility: CompatibilityScore


@dataclass(frozen=True, slots=True)
class _PreviewExport:
    path: pathlib.Path
    sha256: str


def _write_preview(
    staging_dir: pathlib.Path,
    events_document: dict[str, Any],
    requested: bool,
) -> _PreviewExport | None:
    if not requested or not events_document["events"]:
        return None
    synthesis = synthesize_preview_wav(
        events_document,
        staging_dir / PREVIEW_NAME,
        overwrite=True,
    )
    return _PreviewExport(path=synthesis.path, sha256=sha256_file(synthesis.path))


def _preview_artifact_entries(preview: _PreviewExport | None) -> list[dict[str, Any]]:
    if preview is None:
        return []
    return [
        {
            "kind": "preview_wav",
            "relative_path": PREVIEW_NAME,
            "sha256": preview.sha256,
            "size_bytes": preview.path.stat().st_size,
        }
    ]


def _artifact_entry(kind: str, path: pathlib.Path) -> dict[str, Any]:
    return {
        "kind": kind,
        "relative_path": path.name,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _render_performance_for_mapping(
    staging_dir: pathlib.Path,
    mapping: Any,
    *,
    source_type: str,
    revision_source: str,
    source_offset_us: int,
    title: str,
    timing_mode: str,
    preview_wav: bool,
) -> tuple[PerformanceDocument, PerformanceBundle]:
    document = build_performance(
        mapping,
        source_type=cast(Any, source_type),
        revision_source=cast(Any, revision_source),
        source_offset_us=source_offset_us,
    )
    bundle = render_performance_bundle(
        document,
        staging_dir,
        title=title,
        timing_mode=timing_mode,
        preview_wav=preview_wav,
        generator=f"GenshinLyreTranscriber {__version__}",
    )
    return document, bundle


def _performance_artifacts(bundle: PerformanceBundle) -> list[dict[str, Any]]:
    return [
        artifact
        for artifact in bundle.artifacts
        if artifact["kind"] in {"performance", "performance_midi"}
    ]


def _read_optional_report(source_dir: pathlib.Path) -> dict[str, Any]:
    report_path = source_dir / REPORT_NAME
    if not report_path.is_file():
        return {}
    try:
        document = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _report_input_filename(report: dict[str, Any]) -> str | None:
    input_document = report.get("input")
    if not isinstance(input_document, dict):
        return None
    filename = input_document.get("filename")
    return filename if isinstance(filename, str) and filename else None


def _performance_report_input(
    report: dict[str, Any],
    *,
    performance_path: pathlib.Path,
    document: PerformanceDocument,
    fallback_title: str,
) -> dict[str, Any]:
    previous = report.get("input")
    previous = previous if isinstance(previous, dict) else {}
    sha256 = previous.get("sha256")
    if not _is_sha256(sha256):
        sha256 = sha256_file(performance_path)
    offset = previous.get("segment_start_us")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        offset = document.source.offset_us
    return {
        "source_type": document.source.type,
        "filename": _report_input_filename(report) or fallback_title,
        "sha256": sha256,
        "segment_start_us": offset,
    }


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _write_text_scores(
    staging_dir: pathlib.Path,
    sequence: NoteSequence,
    events_document: dict[str, Any],
    *,
    title: str,
    timing_mode: str,
    transpose_semitones: int,
    mapping_profile: str,
    source_offset_us: int,
) -> _TextExports:
    readable_text = build_readable_score(
        sequence,
        events_document,
        title=title,
        timing_mode=timing_mode,
        transpose_semitones=transpose_semitones,
        mapping_profile=mapping_profile,
        source_offset_us=source_offset_us,
    )
    readable_path = write_text_score(
        readable_text,
        staging_dir / READABLE_NAME,
        overwrite=True,
    )
    compatibility = build_compatibility_score(events_document)
    compatibility_path = write_text_score(
        compatibility.text,
        staging_dir / COMPAT_NAME,
        overwrite=True,
    )
    return _TextExports(
        readable_path=readable_path,
        readable_hash=sha256_file(readable_path),
        compatibility_path=compatibility_path,
        compatibility_hash=sha256_file(compatibility_path),
        compatibility=compatibility,
    )


def _preview_warnings(
    requested: bool,
    preview: _PreviewExport | None,
    events_document: dict[str, Any],
) -> list[dict[str, str]]:
    if requested and preview is None and not events_document["events"]:
        return [
            {
                "code": "EMPTY_PREVIEW",
                "message": "empty score has no audible preview",
            }
        ]
    return []


def _compatibility_warnings(compatibility: CompatibilityScore) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    if compatibility.collisions:
        warnings.append(
            {
                "code": "COMPATIBILITY_COLLISIONS",
                "message": (
                    "legacy 10ms grid could not preserve "
                    f"{compatibility.collisions} retrigger event(s)"
                ),
            }
        )
    if compatibility.omitted_tail_us:
        warnings.append(
            {
                "code": "COMPATIBILITY_TAIL_OMITTED",
                "message": (
                    f"legacy score omits {compatibility.omitted_tail_us} us of trailing silence"
                ),
            }
        )
    return warnings


def _cleaning_config() -> CleanConfig:
    profile = os.environ.get("GLT_CLEANING_PROFILE", "auto").strip().lower()
    presets = {
        "solo": (0.2, 50_000, 30_000),
        "mix": (0.4, 100_000, 30_000),
        "strict": (0.5, 150_000, 30_000),
    }
    if profile == "auto":
        min_confidence, min_duration_us, retrigger_gap_us = (0.2, 50_000, 30_000)
    elif profile in presets:
        min_confidence, min_duration_us, retrigger_gap_us = presets[profile]
    else:
        raise WorkerJobError(
            "INVALID_CLEANING_OPTIONS",
            "cleaning profile must be auto, solo, mix or strict",
        )
    try:
        if "GLT_MIN_CONFIDENCE" in os.environ:
            min_confidence = float(os.environ["GLT_MIN_CONFIDENCE"])
        if "GLT_MIN_DURATION_US" in os.environ:
            min_duration_us = int(os.environ["GLT_MIN_DURATION_US"])
        if "GLT_RETRIGGER_GAP_US" in os.environ:
            retrigger_gap_us = int(os.environ["GLT_RETRIGGER_GAP_US"])
    except ValueError as exc:
        raise WorkerJobError("INVALID_CLEANING_OPTIONS", "cleaning options are invalid") from exc
    config = CleanConfig(
        min_confidence=min_confidence,
        min_duration_us=min_duration_us,
        retrigger_gap_us=retrigger_gap_us,
    )
    try:
        config.validate()
    except ValueError as exc:
        raise WorkerJobError("INVALID_CLEANING_OPTIONS", str(exc)) from exc
    return config


def _arrangement_config() -> ArrangementConfig:
    profile = os.environ.get("GLT_ARRANGEMENT", "off").strip().lower()
    if profile not in {"balanced", "off"}:
        raise WorkerJobError(
            "INVALID_ARRANGEMENT_OPTIONS",
            "arrangement profile must be balanced or off",
        )
    try:
        onset_window_us = int(os.environ.get("GLT_ONSET_WINDOW_US", "150000"))
        max_voices = int(os.environ.get("GLT_MAX_VOICES", "2"))
    except ValueError as exc:
        raise WorkerJobError(
            "INVALID_ARRANGEMENT_OPTIONS",
            "arrangement options are invalid",
        ) from exc
    config = ArrangementConfig(
        enabled=profile == "balanced",
        onset_window_us=onset_window_us,
        max_voices=max_voices,
    )
    try:
        config.validate()
    except ValueError as exc:
        raise WorkerJobError("INVALID_ARRANGEMENT_OPTIONS", str(exc)) from exc
    return config


def _arrangement_config_document(config: ArrangementConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "onset_window_us": config.onset_window_us,
        "max_voices": config.max_voices,
    }


def _arrangement_config_from_document(document: Any) -> ArrangementConfig:
    if not isinstance(document, dict):
        raise WorkerJobError("CACHE_INVALID", "candidate arrangement config is invalid")
    enabled = document.get("enabled")
    onset_window_us = document.get("onset_window_us")
    max_voices = document.get("max_voices")
    if (
        not isinstance(enabled, bool)
        or isinstance(onset_window_us, bool)
        or not isinstance(onset_window_us, int)
        or isinstance(max_voices, bool)
        or not isinstance(max_voices, int)
    ):
        raise WorkerJobError("CACHE_INVALID", "candidate arrangement config is invalid")
    config = ArrangementConfig(
        enabled=enabled,
        onset_window_us=onset_window_us,
        max_voices=max_voices,
    )
    try:
        config.validate()
    except ValueError as exc:
        raise WorkerJobError("CACHE_INVALID", str(exc)) from exc
    return config


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


def _raise_if_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise TranscriptionCancelled()


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


def _report_warnings(
    cleaning_removed: int,
    timing_fallback: bool,
    quantization_fallbacks: int,
    cleaning_config: CleanConfig,
    arrangement_config: ArrangementConfig,
    arrangement_removed: int,
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = [
        {
            "code": "CLEANING_CONFIG",
            "message": (
                "cleaning thresholds: "
                f"profile={os.environ.get('GLT_CLEANING_PROFILE', 'auto')}, "
                f"min_confidence={cleaning_config.min_confidence}, "
                f"min_duration_us={cleaning_config.min_duration_us}, "
                f"retrigger_gap_us={cleaning_config.retrigger_gap_us}"
            ),
        },
        {
            "code": "ARRANGEMENT_CONFIG",
            "message": (
                "arrangement: "
                f"profile={'balanced' if arrangement_config.enabled else 'off'}, "
                f"onset_window_us={arrangement_config.onset_window_us}, "
                f"max_voices={arrangement_config.max_voices}"
            ),
        },
    ]
    if cleaning_removed:
        warnings.append(
            {
                "code": "CLEANING_LOSS",
                "message": f"cleaning removed {cleaning_removed} notes",
            }
        )
    if arrangement_removed:
        warnings.append(
            {
                "code": "ARRANGEMENT_LOSS",
                "message": f"playability arrangement removed {arrangement_removed} notes",
            }
        )
    if timing_fallback:
        warnings.append(
            {
                "code": "TIMING_FALLBACK",
                "message": "automatic timing was not used; original onsets were preserved",
            }
        )
    if quantization_fallbacks:
        warnings.append(
            {
                "code": "QUANTIZATION_FALLBACK",
                "message": f"{quantization_fallbacks} notes kept their original timing",
            }
        )
    return warnings


def _refilter_warnings(
    filter_removed: int,
    timing_fallback: bool,
    quantization_fallbacks: int,
    arrangement_config: ArrangementConfig,
    arrangement_removed: int,
    automatic_diagnostics: dict[str, Any] | None,
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = [
        {
            "code": "FILTER_CONFIG",
            "message": "grouped note filter applied from candidate cache",
        },
        {
            "code": "ARRANGEMENT_CONFIG",
            "message": (
                "arrangement: "
                f"profile={'balanced' if arrangement_config.enabled else 'off'}, "
                f"onset_window_us={arrangement_config.onset_window_us}, "
                f"max_voices={arrangement_config.max_voices}"
            ),
        },
    ]
    if filter_removed:
        warnings.append(
            {
                "code": "FILTER_LOSS",
                "message": f"note filter removed {filter_removed} notes",
            }
        )
    if arrangement_removed:
        warnings.append(
            {
                "code": "ARRANGEMENT_LOSS",
                "message": f"playability arrangement removed {arrangement_removed} notes",
            }
        )
    if timing_fallback:
        warnings.append(
            {
                "code": "TIMING_FALLBACK",
                "message": "candidate cache contains preserved timing without an estimated grid",
            }
        )
    if quantization_fallbacks:
        warnings.append(
            {
                "code": "QUANTIZATION_FALLBACK",
                "message": f"{quantization_fallbacks} notes kept their original timing",
            }
        )
    if automatic_diagnostics is not None:
        warnings.append(
            {
                "code": "FILTER_AUTO",
                "message": (
                    "automatic filter thresholds: "
                    f"duration_floor_ms={automatic_diagnostics.get('duration_floor_ms')}, "
                    f"confidence_floor={automatic_diagnostics.get('confidence_floor')}, "
                    f"pitch_floor={automatic_diagnostics.get('pitch_floor')}, "
                    f"suspected_notes={automatic_diagnostics.get('suspected_notes')}"
                ),
            }
        )
    return warnings


def _filter_selection(
    options: dict[str, Any],
    *,
    cache_sequence: Any,
) -> tuple[FilterSpec, dict[str, Any] | None]:
    filter_document = options.get("filter")
    if filter_document is not None:
        return filter_spec_from_dict(filter_document), None
    preset = options.get("filter_preset")
    if not isinstance(preset, str):
        raise ValueError("refilter requires filter or filter_preset")
    if cache_sequence is None:
        raise ValueError("automatic filter detection requires candidate notes")
    recommendation = filter_preset(preset, cache_sequence)
    return recommendation.spec, recommendation.diagnostics


def _quantization_config(options: dict[str, Any], *, default_mode: str) -> QuantizationConfig:
    mode = str(options.get("timing", default_mode))
    bpm_value = options.get("bpm")
    try:
        bpm = float(bpm_value) if bpm_value is not None else None
        config = QuantizationConfig(mode=mode, bpm=bpm)  # type: ignore[arg-type]
        config.validate()
    except (TypeError, ValueError) as exc:
        raise WorkerJobError("SCHEMA_INVALID", f"invalid timing options: {exc}") from exc
    return config


def _mapping_config(options: dict[str, Any]) -> MappingConfig:
    transpose: object = options.get("transpose", "auto")
    if not (
        transpose == "auto" or (isinstance(transpose, int) and not isinstance(transpose, bool))
    ):
        raise WorkerJobError("SCHEMA_INVALID", "transpose must be 'auto' or an integer")
    config = MappingConfig(layout=default_mapping_layout(), transpose=transpose)
    try:
        config.validate()
    except ValueError as exc:
        raise WorkerJobError("SCHEMA_INVALID", f"invalid transpose: {exc}") from exc
    return config


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        return analysis_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "separate":
        return separation_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "separate-demucs":
        return demucs_main(sys.argv[2:])
    logging.basicConfig(level=logging.WARNING)
    return WorkerServer(sys.stdin, sys.stdout).run()


if __name__ == "__main__":
    raise SystemExit(main())
