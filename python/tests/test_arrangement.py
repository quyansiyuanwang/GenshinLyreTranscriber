from __future__ import annotations

from glt_core.domain.note_sequence import Note, NoteSequence, Provenance
from glt_core.processing.arrangement import ArrangementConfig, arrange_note_sequence


def _note(
    pitch: int,
    start_us: int,
    *,
    velocity: int = 80,
    duration_us: int = 300_000,
) -> Note:
    return Note(pitch, start_us, start_us + duration_us, velocity, None, 0, 0)


def _sequence(notes: tuple[Note, ...]) -> NoteSequence:
    sequence = NoteSequence(
        duration_us=max(note.end_us for note in notes),
        notes=notes,
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance("audio", 0, "test", {}),
    )
    sequence.validate()
    return sequence


def test_nearby_onsets_are_snapped_and_reduced_to_two_voices() -> None:
    sequence = _sequence(
        (
            _note(69, 10_000, velocity=100),
            _note(81, 40_000, velocity=95),
            _note(76, 80_000, velocity=90),
        )
    )
    result = arrange_note_sequence(
        sequence,
        ArrangementConfig(onset_window_us=100_000, max_voices=2),
    )
    assert [note.pitch for note in result.arranged.notes] == [69, 76]
    assert {note.start_us for note in result.arranged.notes} == {10_000}
    assert result.stats.input_notes == 3
    assert result.stats.output_notes == 2
    assert result.stats.dropped_notes == 1
    assert result.stats.output_events == 1


def test_notes_outside_window_remain_separate_events() -> None:
    sequence = _sequence((_note(60, 0), _note(64, 200_000)))
    result = arrange_note_sequence(
        sequence,
        ArrangementConfig(onset_window_us=100_000, max_voices=1),
    )
    assert [note.start_us for note in result.arranged.notes] == [0, 200_000]
    assert result.stats.output_events == 2


def test_disabled_arrangement_preserves_notes() -> None:
    sequence = _sequence((_note(60, 0), _note(64, 10_000)))
    result = arrange_note_sequence(sequence, ArrangementConfig(enabled=False))
    assert result.arranged is sequence
    assert result.stats.dropped_notes == 0
