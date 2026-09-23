from __future__ import annotations

from glt_core.domain.note_sequence import Note, NoteSequence, Provenance
from glt_core.processing.clean import CleanConfig, clean_note_sequence


def _sequence(notes: tuple[Note, ...], duration_us: int = 2_000_000) -> NoteSequence:
    return NoteSequence(
        duration_us=duration_us,
        notes=notes,
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance("audio", 0, "test", {"source": "unit"}),
    )


def _note(
    pitch: int,
    start: int,
    end: int,
    *,
    velocity: int = 100,
    confidence: float | None = 0.9,
) -> Note:
    return Note(pitch, start, end, velocity, confidence, 0, 0)


def test_cleaning_is_deterministic_and_accounted() -> None:
    notes = (
        _note(60, 0, 40_000),
        _note(61, 100_000, 300_000),
        _note(62, 400_000, 800_000, confidence=0.1),
        _note(63, 900_000, 1_200_000),
        _note(63, 900_000, 1_200_000, velocity=120),
    )
    sequence = _sequence(notes)
    first = clean_note_sequence(sequence)
    second = clean_note_sequence(sequence)
    assert first.cleaned == second.cleaned
    assert first.stats == second.stats
    assert first.stats.input_notes == 5
    assert first.stats.output_notes == 2
    assert first.stats.dropped_short == 1
    assert first.stats.dropped_low_confidence == 1
    assert first.stats.duplicate_notes_removed == 1
    assert first.cleaned.notes[1].velocity == 120
    assert {change.reason for change in first.changes} == {
        "short_duration",
        "low_confidence",
        "exact_duplicate",
    }
    assert first.cleaned.provenance.parameters["cleaning"]["min_duration_us"] == 50_000


def test_retriggers_are_preserved_and_close_overlaps_are_merged() -> None:
    sequence = _sequence(
        (
            _note(60, 0, 500_000),
            _note(60, 10_000, 700_000),  # close start and overlap: merge
            _note(60, 800_000, 1_000_000),  # separate attack: preserve
            _note(60, 810_000, 1_100_000),  # close retrigger: merge
        )
    )
    result = clean_note_sequence(sequence)
    assert [(note.start_us, note.end_us) for note in result.cleaned.notes] == [
        (0, 700_000),
        (800_000, 1_100_000),
    ]
    assert result.stats.overlapped_notes_merged == 2
    assert result.stats.retriggers_preserved == 1


def test_all_filtered_is_valid_empty_sequence() -> None:
    sequence = _sequence(
        (_note(60, 0, 20_000), _note(62, 100_000, 150_000, confidence=0.0)),
        duration_us=750_000,
    )
    result = clean_note_sequence(sequence, CleanConfig(min_confidence=0.2, min_duration_us=50_000))
    assert result.cleaned.notes == ()
    assert result.cleaned.duration_us == 750_000
    assert result.stats.output_notes == 0
