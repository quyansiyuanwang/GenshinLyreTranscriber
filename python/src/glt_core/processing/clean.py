"""Deterministic note cleaning with accounting for every modification."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Literal

from glt_core.domain.note_sequence import Note, NoteSequence, Provenance

ChangeAction = Literal["drop", "deduplicate", "merge"]


@dataclass(frozen=True, slots=True)
class CleanConfig:
    min_confidence: float = 0.2
    min_duration_us: int = 50_000
    retrigger_gap_us: int = 30_000

    def validate(self) -> None:
        if not 0 <= self.min_confidence <= 1:
            raise ValueError("min_confidence must be in range 0..1")
        if self.min_duration_us < 0:
            raise ValueError("min_duration_us must be non-negative")
        if self.retrigger_gap_us < 0:
            raise ValueError("retrigger_gap_us must be non-negative")


@dataclass(frozen=True, slots=True)
class NoteChange:
    original_index: int
    action: ChangeAction
    reason: str
    pitch: int
    start_us: int


@dataclass(frozen=True, slots=True)
class CleanStats:
    input_notes: int
    output_notes: int
    dropped_low_confidence: int
    dropped_short: int
    duplicate_notes_removed: int
    overlapped_notes_merged: int
    retriggers_preserved: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CleanResult:
    original: NoteSequence
    cleaned: NoteSequence
    stats: CleanStats
    changes: tuple[NoteChange, ...]


def clean_note_sequence(
    sequence: NoteSequence,
    config: CleanConfig | None = None,
) -> CleanResult:
    """Apply configured filters and duplicate/overlap handling deterministically."""
    selected = config or CleanConfig()
    selected.validate()
    sequence.validate()
    indexed = list(enumerate(sequence.notes))
    changes: list[NoteChange] = []
    candidates: list[tuple[int, Note]] = []
    dropped_low_confidence = 0
    dropped_short = 0

    for index, note in sorted(indexed, key=lambda item: _sort_key(item[1], item[0])):
        if note.confidence is not None and note.confidence < selected.min_confidence:
            dropped_low_confidence += 1
            changes.append(_change(index, "drop", "low_confidence", note))
            continue
        if note.end_us - note.start_us < selected.min_duration_us:
            dropped_short += 1
            changes.append(_change(index, "drop", "short_duration", note))
            continue
        candidates.append((index, note))

    deduplicated: list[tuple[int, Note]] = []
    for index, note in candidates:
        duplicate_index = next(
            (
                position
                for position, (existing_index, existing) in enumerate(deduplicated)
                if _same_note_identity(existing, note)
            ),
            None,
        )
        if duplicate_index is None:
            deduplicated.append((index, note))
            continue
        existing_index, existing = deduplicated[duplicate_index]
        if (note.velocity, note.confidence or -1.0) > (
            existing.velocity,
            existing.confidence or -1.0,
        ):
            deduplicated[duplicate_index] = (index, note)
            changes.append(_change(existing_index, "deduplicate", "exact_duplicate", existing))
        else:
            changes.append(_change(index, "deduplicate", "exact_duplicate", note))

    cleaned_notes: list[Note] = []
    retriggers_preserved = 0
    overlaps_merged = 0
    for index, note in deduplicated:
        previous_index = _find_previous(cleaned_notes, note)
        if previous_index is None:
            cleaned_notes.append(note)
            continue
        previous = cleaned_notes[previous_index]
        close_start = note.start_us - previous.start_us < selected.retrigger_gap_us
        overlaps = note.start_us < previous.end_us
        if close_start and overlaps:
            merged = replace(
                previous,
                end_us=max(previous.end_us, note.end_us),
                velocity=max(previous.velocity, note.velocity),
                confidence=_max_confidence(previous.confidence, note.confidence),
            )
            cleaned_notes[previous_index] = merged
            overlaps_merged += 1
            changes.append(_change(index, "merge", "overlapping_duplicate", note))
        else:
            cleaned_notes.append(note)
            if note.pitch == previous.pitch:
                retriggers_preserved += 1

    cleaned_notes.sort(key=lambda note: _sort_key(note, 0))
    parameters = dict(sequence.provenance.parameters)
    parameters["cleaning"] = {
        "min_confidence": selected.min_confidence,
        "min_duration_us": selected.min_duration_us,
        "retrigger_gap_us": selected.retrigger_gap_us,
    }
    cleaned = NoteSequence(
        schema_version=sequence.schema_version,
        time_unit=sequence.time_unit,
        duration_us=sequence.duration_us,
        notes=tuple(cleaned_notes),
        tempo_map=sequence.tempo_map,
        beat_grid=sequence.beat_grid,
        provenance=Provenance(
            source_type=sequence.provenance.source_type,
            source_offset_us=sequence.provenance.source_offset_us,
            model_version=sequence.provenance.model_version,
            parameters=parameters,
        ),
    )
    cleaned.validate()
    stats = CleanStats(
        input_notes=len(sequence.notes),
        output_notes=len(cleaned.notes),
        dropped_low_confidence=dropped_low_confidence,
        dropped_short=dropped_short,
        duplicate_notes_removed=len(
            [change for change in changes if change.action == "deduplicate"]
        ),
        overlapped_notes_merged=overlaps_merged,
        retriggers_preserved=retriggers_preserved,
    )
    _validate_accounting(stats)
    return CleanResult(
        original=sequence,
        cleaned=cleaned,
        stats=stats,
        changes=tuple(changes),
    )


def _sort_key(note: Note, original_index: int) -> tuple[int, int, int, int, int, int]:
    return (note.start_us, note.pitch, note.track, note.channel, note.end_us, original_index)


def _same_note_identity(left: Note, right: Note) -> bool:
    return (
        left.pitch,
        left.start_us,
        left.end_us,
        left.track,
        left.channel,
    ) == (
        right.pitch,
        right.start_us,
        right.end_us,
        right.track,
        right.channel,
    )


def _find_previous(notes: list[Note], current: Note) -> int | None:
    for index in range(len(notes) - 1, -1, -1):
        candidate = notes[index]
        if (
            candidate.pitch == current.pitch
            and candidate.track == current.track
            and candidate.channel == current.channel
        ):
            return index
    return None


def _max_confidence(left: float | None, right: float | None) -> float | None:
    values = [value for value in (left, right) if value is not None]
    return max(values) if values else None


def _change(index: int, action: ChangeAction, reason: str, note: Note) -> NoteChange:
    return NoteChange(
        original_index=index,
        action=action,
        reason=reason,
        pitch=note.pitch,
        start_us=note.start_us,
    )


def _validate_accounting(stats: CleanStats) -> None:
    removed = (
        stats.dropped_low_confidence
        + stats.dropped_short
        + stats.duplicate_notes_removed
        + stats.overlapped_notes_merged
    )
    if stats.input_notes - removed != stats.output_notes:
        raise AssertionError("cleaning statistics do not match the note count")
