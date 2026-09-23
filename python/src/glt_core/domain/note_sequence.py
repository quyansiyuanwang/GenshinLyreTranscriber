"""Validated internal note timeline used by all processing stages."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal

from glt_core.protocol.validation import ProtocolValidationError

TempoSource = Literal["midi", "estimated", "manual"]
SourceType = Literal["audio", "video", "midi"]
SAFE_INTEGER_MAX = 9_007_199_254_740_991


@dataclass(frozen=True, slots=True)
class Note:
    pitch: int
    start_us: int
    end_us: int
    velocity: int
    confidence: float | None
    track: int
    channel: int


@dataclass(frozen=True, slots=True)
class TempoPoint:
    at_us: int
    bpm: float
    source: TempoSource


@dataclass(frozen=True, slots=True)
class BeatGridPoint:
    at_us: int
    beat_position: float
    bpm: float
    confidence: float | None


@dataclass(frozen=True, slots=True)
class Provenance:
    source_type: SourceType
    source_offset_us: int
    model_version: str | None
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NoteSequence:
    duration_us: int
    notes: tuple[Note, ...]
    tempo_map: tuple[TempoPoint, ...]
    beat_grid: tuple[BeatGridPoint, ...]
    provenance: Provenance
    schema_version: int = 1
    time_unit: Literal["us"] = "us"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "time_unit": self.time_unit,
            "duration_us": self.duration_us,
            "notes": [asdict(note) for note in self.notes],
            "tempo_map": [asdict(point) for point in self.tempo_map],
            "beat_grid": [asdict(point) for point in self.beat_grid],
            "provenance": asdict(self.provenance),
        }

    def validate(self) -> None:
        if self.schema_version != 1 or self.time_unit != "us":
            raise ProtocolValidationError(
                "UNSUPPORTED_VERSION", "NoteSequence must use schema version 1 and microseconds"
            )
        if not 0 <= self.duration_us <= SAFE_INTEGER_MAX:
            raise ProtocolValidationError(
                "NOTE_RANGE", "duration_us is outside the safe integer range"
            )
        previous: tuple[int, int, int] | None = None
        for note in self.notes:
            if not 0 <= note.start_us < note.end_us <= self.duration_us:
                raise ProtocolValidationError(
                    "NOTE_RANGE", "note start/end is outside the sequence duration"
                )
            if not 0 <= note.pitch <= 127:
                raise ProtocolValidationError("NOTE_RANGE", "pitch must be in MIDI range 0..127")
            if not 1 <= note.velocity <= 127:
                raise ProtocolValidationError("NOTE_RANGE", "velocity must be in range 1..127")
            if note.confidence is not None and (
                not math.isfinite(note.confidence) or not 0 <= note.confidence <= 1
            ):
                raise ProtocolValidationError(
                    "NOTE_RANGE", "confidence must be null or in range 0..1"
                )
            if note.track < 0 or not 0 <= note.channel <= 15:
                raise ProtocolValidationError("NOTE_RANGE", "track/channel identifiers are invalid")
            order = (note.start_us, note.pitch, note.track)
            if previous is not None and order < previous:
                raise ProtocolValidationError("NOTE_ORDER", "notes are not sorted")
            previous = order
        previous_tempo: int | None = None
        for tempo_point in self.tempo_map:
            if not 0 <= tempo_point.at_us <= SAFE_INTEGER_MAX:
                raise ProtocolValidationError("TEMPO_ORDER", "tempo time is invalid")
            if previous_tempo is not None and tempo_point.at_us < previous_tempo:
                raise ProtocolValidationError("TEMPO_ORDER", "tempo map is not sorted")
            if tempo_point.source not in {"midi", "estimated", "manual"}:
                raise ProtocolValidationError("TEMPO_ORDER", "tempo source is invalid")
            if not math.isfinite(tempo_point.bpm) or not 0 < tempo_point.bpm <= 1000:
                raise ProtocolValidationError("TEMPO_ORDER", "tempo BPM is invalid")
            previous_tempo = tempo_point.at_us
        previous_beat: int | None = None
        for beat_point in self.beat_grid:
            if not 0 <= beat_point.at_us <= SAFE_INTEGER_MAX:
                raise ProtocolValidationError("BEAT_ORDER", "beat time is invalid")
            if previous_beat is not None and beat_point.at_us < previous_beat:
                raise ProtocolValidationError("BEAT_ORDER", "beat grid is not sorted")
            if not math.isfinite(beat_point.beat_position):
                raise ProtocolValidationError("BEAT_ORDER", "beat position is not finite")
            if not math.isfinite(beat_point.bpm) or not 0 < beat_point.bpm <= 1000:
                raise ProtocolValidationError("BEAT_ORDER", "beat BPM is invalid")
            if beat_point.confidence is not None and (
                not math.isfinite(beat_point.confidence) or not 0 <= beat_point.confidence <= 1
            ):
                raise ProtocolValidationError("BEAT_ORDER", "confidence is invalid")
            previous_beat = beat_point.at_us
        if self.provenance.source_type not in {"audio", "video", "midi"}:
            raise ProtocolValidationError("SCHEMA_INVALID", "source type is invalid")
        if not 0 <= self.provenance.source_offset_us <= SAFE_INTEGER_MAX:
            raise ProtocolValidationError("SCHEMA_INVALID", "source offset is invalid")
