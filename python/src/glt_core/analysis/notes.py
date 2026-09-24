"""Versioned analysis note events, pitch bends, velocity and MIDI serialization."""

from __future__ import annotations

import json
import math
import os
import pathlib
from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np
import pretty_midi

from glt_core.transcription.basic_pitch import NoteEvent

BendClass = Literal["stable", "vibrato", "slide", "bend"]


@dataclass(frozen=True, slots=True)
class PitchBendPoint:
    at_us: int
    cents: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AnalysisNote:
    id: str
    start_us: int
    end_us: int
    pitch: int
    velocity: int
    confidence: float
    source_stem: str
    pitch_center: float
    pitch_bend_class: BendClass
    pitch_bends: tuple[PitchBendPoint, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "pitch": self.pitch,
            "velocity": self.velocity,
            "confidence": self.confidence,
            "source_stem": self.source_stem,
            "pitch_center": self.pitch_center,
            "pitch_bend_class": self.pitch_bend_class,
            "pitch_bends": [point.to_dict() for point in self.pitch_bends],
        }


@dataclass(frozen=True, slots=True)
class AnalysisNoteSet:
    format_version: int
    duration_us: int
    source_stem: str
    notes: tuple[AnalysisNote, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "time_unit": "us",
            "duration_us": self.duration_us,
            "source_stem": self.source_stem,
            "notes": [note.to_dict() for note in self.notes],
        }

    def validate(self) -> None:
        if self.format_version != 1 or not 0 <= self.duration_us <= 9_007_199_254_740_991:
            raise ValueError("invalid analysis note set")
        previous: tuple[int, int, str] | None = None
        for note in self.notes:
            if not 0 <= note.start_us < note.end_us <= self.duration_us:
                raise ValueError(f"analysis note {note.id} has an invalid time range")
            if not 0 <= note.pitch <= 127 or not 1 <= note.velocity <= 127:
                raise ValueError(f"analysis note {note.id} has an invalid pitch/velocity")
            if not math.isfinite(note.confidence) or not 0 <= note.confidence <= 1:
                raise ValueError(f"analysis note {note.id} has invalid confidence")
            order = (note.start_us, note.pitch, note.source_stem)
            if previous is not None and order < previous:
                raise ValueError("analysis notes are not sorted")
            previous = order


def analysis_notes_from_basic_pitch(
    events: tuple[NoteEvent, ...],
    duration_us: int,
    *,
    source_stem: str = "mix",
    source_gain_db: float = 0.0,
) -> AnalysisNoteSet:
    notes: list[AnalysisNote] = []
    for index, (start, end, pitch, amplitude, bends) in enumerate(events):
        start_us = max(0, round(start * 1_000_000))
        end_us = min(duration_us, round(end * 1_000_000))
        if start_us >= end_us:
            continue
        points = _pitch_bend_points(start_us, end_us, bends)
        bend_average = float(np.median([point.cents for point in points])) if points else 0.0
        pitch_center = pitch + bend_average / 100.0
        note = AnalysisNote(
            id=f"{source_stem}-{index:06d}",
            start_us=start_us,
            end_us=end_us,
            pitch=pitch,
            velocity=estimate_velocity(amplitude, end_us - start_us, source_gain_db=source_gain_db),
            confidence=max(0.0, min(1.0, amplitude)),
            source_stem=source_stem,
            pitch_center=round(pitch_center, 6),
            pitch_bend_class=classify_pitch_bend(points),
            pitch_bends=points,
        )
        notes.append(note)
    ordered = tuple(sorted(notes, key=lambda note: (note.start_us, note.pitch, note.id)))
    result = AnalysisNoteSet(1, duration_us, source_stem, ordered)
    result.validate()
    return result


def estimate_velocity(
    amplitude: float,
    duration_us: int,
    *,
    source_gain_db: float = 0.0,
    rms_dbfs: float | None = None,
) -> int:
    if not math.isfinite(amplitude) or amplitude < 0:
        return 1
    gain = 10.0 ** (source_gain_db / 20.0)
    energy = 127.0 * math.sqrt(max(0.0, min(1.0, amplitude * gain)))
    duration_bonus = min(8.0, math.log1p(max(0, duration_us) / 100_000.0) * 3.0)
    rms_adjustment = 0.0 if rms_dbfs is None else max(-10.0, min(10.0, (rms_dbfs + 60.0) / 3.0))
    return max(1, min(127, round(energy + duration_bonus + rms_adjustment)))


def classify_pitch_bend(points: tuple[PitchBendPoint, ...]) -> BendClass:
    if len(points) < 2:
        return "stable"
    cents = np.asarray([point.cents for point in points], dtype=np.float64)
    span = float(np.max(cents) - np.min(cents))
    if span < 10.0:
        return "stable"
    centered = cents - float(np.mean(cents))
    crossings = int(np.sum(np.signbit(centered[:-1]) != np.signbit(centered[1:])))
    differences = np.diff(cents)
    monotonic = bool(np.all(differences >= -0.5) or np.all(differences <= 0.5))
    duration_us = points[-1].at_us - points[0].at_us
    if crossings >= 2 and span <= 150.0:
        return "vibrato"
    if monotonic and duration_us >= 180_000 and span >= 40.0:
        return "slide"
    return "bend"


def write_analysis_notes(
    document: AnalysisNoteSet,
    destination: pathlib.Path | str,
) -> pathlib.Path:
    document.validate()
    output = pathlib.Path(destination).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.partial")
    temporary.unlink(missing_ok=True)
    try:
        temporary.write_text(json.dumps(document.to_dict(), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def write_analysis_midi(document: AnalysisNoteSet, destination: pathlib.Path | str) -> pathlib.Path:
    document.validate()
    output = pathlib.Path(destination).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.partial")
    temporary.unlink(missing_ok=True)
    midi = pretty_midi.PrettyMIDI(initial_tempo=120.0, resolution=960)
    instrument = pretty_midi.Instrument(program=0, name="GenshinLyreTranscriber Analysis")
    midi.instruments.append(instrument)
    for note in document.notes:
        instrument.notes.append(
            pretty_midi.Note(
                velocity=note.velocity,
                pitch=note.pitch,
                start=note.start_us / 1_000_000,
                end=note.end_us / 1_000_000,
            )
        )
        for point in note.pitch_bends:
            pitch_bend = round(max(-8192, min(8191, point.cents * 4096 / 100.0)))
            instrument.pitch_bends.append(
                pretty_midi.PitchBend(pitch_bend, point.at_us / 1_000_000)
            )
    try:
        midi.write(str(temporary))
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def _pitch_bend_points(
    start_us: int,
    end_us: int,
    bends: list[int] | None,
) -> tuple[PitchBendPoint, ...]:
    if not bends:
        return ()
    times = np.linspace(start_us, end_us, len(bends), endpoint=True)
    return tuple(
        PitchBendPoint(round(float(at_us)), bend * 100.0 / 4096.0)
        for at_us, bend in zip(times, bends, strict=True)
    )
