"""Bounded Basic Pitch ONNX transcription with seam fusion."""

from __future__ import annotations

import math
import os
import pathlib
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pretty_midi
import soundfile

from glt_core.domain.note_sequence import Note, NoteSequence, Provenance
from glt_core.transcription.onnx_probe import (
    ModelInfo,
    _load_basic_pitch_api,
    _peak_working_set_bytes,
    inspect_model,
)

DEFAULT_SEGMENT_SECONDS = 15.0
DEFAULT_OVERLAP_SECONDS = 1.0
MIN_NOTE_LENGTH_SECONDS = 0.1277
SEAM_TOLERANCE_SECONDS = 0.03


class TranscriptionError(RuntimeError):
    """A stable transcription failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TranscriptionCancelled(TranscriptionError):
    def __init__(self) -> None:
        super().__init__("CANCELLED", "transcription was cancelled")


NoteEvent = tuple[float, float, int, float, list[int] | None]
ProgressCallback = Callable[[str, float], None]


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    input_path: pathlib.Path
    output_path: pathlib.Path
    model: ModelInfo
    note_count: int
    pitch_min: int | None
    pitch_max: int | None
    segment_count: int
    segment_seconds: float
    overlap_seconds: float
    elapsed_seconds: float
    peak_working_set_bytes: int | None
    output_bytes: int
    note_sequence: NoteSequence


@dataclass(frozen=True, slots=True)
class BasicPitchRuntime:
    model: Any
    model_info: ModelInfo
    model_class: Any
    run_inference: Any
    model_output_to_notes: Any
    sample_rate: int
    fft_hop: int


@dataclass(frozen=True, slots=True)
class _SegmentEvent:
    start: float
    end: float
    pitch: int
    amplitude: float
    pitch_bends: list[int] | None
    segment_index: int

    def as_note_event(self) -> NoteEvent:
        return (self.start, self.end, self.pitch, self.amplitude, self.pitch_bends)


def transcribe_to_midi(
    input_path: pathlib.Path | str,
    output_path: pathlib.Path | str,
    *,
    model_path: pathlib.Path | str | None = None,
    segment_seconds: float = DEFAULT_SEGMENT_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
    runtime: BasicPitchRuntime | None = None,
    progress: ProgressCallback | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> TranscriptionResult:
    """Transcribe a model-format WAV in bounded overlapping segments."""
    if not math.isfinite(segment_seconds) or segment_seconds <= 0:
        raise TranscriptionError("INVALID_SEGMENT", "segment_seconds must be positive")
    if (
        not math.isfinite(overlap_seconds)
        or overlap_seconds < 0
        or overlap_seconds >= segment_seconds
    ):
        raise TranscriptionError(
            "INVALID_SEGMENT",
            "overlap_seconds must be non-negative and smaller than segment_seconds",
        )
    source = pathlib.Path(input_path).expanduser().resolve()
    destination = pathlib.Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise TranscriptionError("INPUT_NOT_FOUND", f"input audio does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    runtime = runtime or load_basic_pitch_runtime(model_path)
    model = runtime.model
    model_info = runtime.model_info
    run_inference = runtime.run_inference
    model_output_to_notes = runtime.model_output_to_notes
    sample_rate = runtime.sample_rate
    fft_hop = runtime.fft_hop
    try:
        info = soundfile.info(source)
    except soundfile.SoundFileRuntimeError as exc:
        raise TranscriptionError("INPUT_DECODE_FAILED", str(exc)) from exc
    if info.samplerate != sample_rate:
        raise TranscriptionError(
            "AUDIO_FORMAT",
            f"input sample rate must be {sample_rate}; got {info.samplerate}",
        )
    if info.channels != 1:
        raise TranscriptionError(
            "AUDIO_FORMAT",
            f"input must be mono; got {info.channels} channels",
        )

    segment_frames = max(1, round(segment_seconds * sample_rate))
    overlap_frames = max(0, round(overlap_seconds * sample_rate))
    hop_frames = segment_frames - overlap_frames
    segment_count = max(1, math.ceil(max(0, info.frames - overlap_frames) / hop_frames))
    min_note_len = int(round(MIN_NOTE_LENGTH_SECONDS * (sample_rate / fft_hop)))
    events: list[_SegmentEvent] = []
    actual_segments = 0

    with tempfile.TemporaryDirectory(prefix=".glt-segments-", dir=destination.parent) as temporary:
        segment_path = pathlib.Path(temporary) / "segment.wav"
        for segment_index in range(segment_count):
            _raise_if_cancelled(cancelled)
            start_frame = segment_index * hop_frames
            if start_frame >= info.frames:
                break
            frames = min(segment_frames, info.frames - start_frame)
            data, actual_rate = soundfile.read(
                source,
                start=start_frame,
                frames=frames,
                dtype="float32",
                always_2d=False,
            )
            if actual_rate != sample_rate:
                raise TranscriptionError("AUDIO_FORMAT", "soundfile returned an unexpected rate")
            soundfile.write(segment_path, data, sample_rate, subtype="PCM_16")
            output = run_inference(segment_path, model)
            _midi, local_events = model_output_to_notes(
                output,
                onset_thresh=0.5,
                frame_thresh=0.3,
                min_note_len=min_note_len,
                min_freq=None,
                max_freq=None,
                multiple_pitch_bends=False,
                melodia_trick=True,
                midi_tempo=120,
            )
            offset = start_frame / sample_rate
            for start, end, pitch, amplitude, bends in local_events:
                if not _valid_event(start, end, pitch, amplitude):
                    continue
                events.append(
                    _SegmentEvent(
                        start=max(0.0, float(start) + offset),
                        end=float(end) + offset,
                        pitch=int(pitch),
                        amplitude=float(amplitude),
                        pitch_bends=bends,
                        segment_index=segment_index,
                    )
                )
            actual_segments += 1
            if progress is not None:
                progress("transcribing", (segment_index + 1) / segment_count)
            _raise_if_cancelled(cancelled)

    fused = fuse_segment_events(events)
    midi_data = _events_to_midi(fused)
    duration_us = round(info.frames / sample_rate * 1_000_000)
    note_sequence = _events_to_note_sequence(fused, duration_us, model_info)
    partial = destination.with_name(f".{destination.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        midi_data.write(str(partial))
        if not partial.is_file():
            raise TranscriptionError("EXPORT_FAILED", "MIDI writer did not create output")
        os.replace(partial, destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    pitches = [event[2] for event in fused]
    return TranscriptionResult(
        input_path=source,
        output_path=destination,
        model=model_info,
        note_count=len(fused),
        pitch_min=min(pitches) if pitches else None,
        pitch_max=max(pitches) if pitches else None,
        segment_count=actual_segments,
        segment_seconds=segment_seconds,
        overlap_seconds=overlap_seconds,
        elapsed_seconds=round(time.perf_counter() - started, 6),
        peak_working_set_bytes=_peak_working_set_bytes(),
        output_bytes=destination.stat().st_size,
        note_sequence=note_sequence,
    )


def load_basic_pitch_runtime(
    model_path: pathlib.Path | str | None = None,
) -> BasicPitchRuntime:
    """Load ONNX and Basic Pitch APIs on the calling thread."""
    model, model_info = inspect_model(model_path)
    model_class, run_inference, model_output_to_notes, runtime = _load_basic_pitch_api()
    _onnxruntime, sample_rate, fft_hop = runtime
    return BasicPitchRuntime(
        model=model,
        model_info=model_info,
        model_class=model_class,
        run_inference=run_inference,
        model_output_to_notes=model_output_to_notes,
        sample_rate=int(sample_rate),
        fft_hop=int(fft_hop),
    )


def fuse_segment_events(events: list[_SegmentEvent]) -> list[NoteEvent]:
    """Merge split notes only across distinct source segments, preserving retriggers."""
    ordered = sorted(events, key=lambda event: (event.start, event.pitch, event.segment_index))
    fused: list[_SegmentEvent] = []
    last_by_pitch: dict[int, int] = {}
    last_segment_by_pitch: dict[int, int] = {}
    for event in ordered:
        previous_index = last_by_pitch.get(event.pitch)
        if previous_index is None:
            last_by_pitch[event.pitch] = len(fused)
            last_segment_by_pitch[event.pitch] = event.segment_index
            fused.append(event)
            continue
        previous = fused[previous_index]
        can_merge = (
            event.segment_index != last_segment_by_pitch[event.pitch]
            and event.start <= previous.end + SEAM_TOLERANCE_SECONDS
        )
        if not can_merge:
            last_by_pitch[event.pitch] = len(fused)
            last_segment_by_pitch[event.pitch] = event.segment_index
            fused.append(event)
            continue
        if event.end <= previous.end + SEAM_TOLERANCE_SECONDS:
            if event.amplitude > previous.amplitude:
                fused[previous_index] = _SegmentEvent(
                    start=min(previous.start, event.start),
                    end=max(previous.end, event.end),
                    pitch=event.pitch,
                    amplitude=event.amplitude,
                    pitch_bends=event.pitch_bends or previous.pitch_bends,
                    segment_index=previous.segment_index,
                )
            last_segment_by_pitch[event.pitch] = event.segment_index
            continue
        fused[previous_index] = _SegmentEvent(
            start=previous.start,
            end=max(previous.end, event.end),
            pitch=previous.pitch,
            amplitude=max(previous.amplitude, event.amplitude),
            pitch_bends=event.pitch_bends or previous.pitch_bends,
            segment_index=previous.segment_index,
        )
        last_segment_by_pitch[event.pitch] = event.segment_index
    return [event.as_note_event() for event in fused]


def _events_to_midi(events: list[NoteEvent]) -> pretty_midi.PrettyMIDI:
    midi = pretty_midi.PrettyMIDI(initial_tempo=120.0)
    instrument = pretty_midi.Instrument(program=0, name="Basic Pitch")
    midi.instruments.append(instrument)
    for start, end, pitch, amplitude, _bends in events:
        velocity = max(1, min(127, round(amplitude * 127)))
        instrument.notes.append(
            pretty_midi.Note(velocity=velocity, pitch=pitch, start=start, end=end)
        )
    return midi


def _events_to_note_sequence(
    events: list[NoteEvent],
    duration_us: int,
    model_info: ModelInfo,
) -> NoteSequence:
    notes: list[Note] = []
    for start, end, pitch, amplitude, _bends in events:
        start_us = round(start * 1_000_000)
        end_us = min(duration_us, round(end * 1_000_000))
        if start_us < 0 or start_us >= end_us:
            continue
        notes.append(
            Note(
                pitch=pitch,
                start_us=start_us,
                end_us=end_us,
                velocity=max(1, min(127, round(amplitude * 127))),
                confidence=max(0.0, min(1.0, amplitude)),
                track=0,
                channel=0,
            )
        )
    sequence = NoteSequence(
        duration_us=duration_us,
        notes=tuple(notes),
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance(
            source_type="audio",
            source_offset_us=0,
            model_version=pathlib.Path(model_info.path).name,
            parameters={"engine": "basic-pitch", "backend": "onnxruntime-cpu"},
        ),
    )
    sequence.validate()
    return sequence


def _valid_event(start: Any, end: Any, pitch: Any, amplitude: Any) -> bool:
    try:
        return (
            math.isfinite(float(start))
            and math.isfinite(float(end))
            and float(end) > float(start)
            and 0 <= int(pitch) <= 127
            and math.isfinite(float(amplitude))
        )
    except (TypeError, ValueError):
        return False


def _raise_if_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise TranscriptionCancelled()
