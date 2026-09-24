from __future__ import annotations

from glt_core.domain.note_sequence import Note, NoteSequence, Provenance
from glt_core.processing.mapping import (
    MappingConfig,
    default_mapping_layout,
    map_note_sequence,
    preview_transpositions,
)


def _sequence(pitches: list[int], *, same_time: bool = False) -> NoteSequence:
    notes = tuple(
        Note(
            pitch=pitch,
            start_us=0 if same_time else index * 500_000,
            end_us=(0 if same_time else index * 500_000) + 300_000,
            velocity=100,
            confidence=None,
            track=0,
            channel=0,
        )
        for index, pitch in enumerate(pitches)
    )
    duration_us = 300_000 if same_time else max(note.end_us for note in notes)
    sequence = NoteSequence(
        duration_us=duration_us,
        notes=notes,
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance("midi", 0, None, {}),
    )
    sequence.validate()
    return sequence


def test_default_layout_covers_all_21_natural_keys() -> None:
    layout = default_mapping_layout()
    assert [entry.key for entry in layout.keys] == list("ZXCVBNMASDFGHJQWERTYU")
    result = map_note_sequence(
        _sequence([entry.pitch for entry in layout.keys]),
        MappingConfig(layout=layout, transpose=0),
    )
    assert result.stats.unique_keys_used == 21
    assert result.stats.replaced_semitones == 0
    assert result.stats.octave_folds == 0


def test_chromatic_tie_octave_fold_and_collision() -> None:
    result = map_note_sequence(
        _sequence([61, 84, 47]),
        MappingConfig(layout=default_mapping_layout(), transpose=0),
    )
    assert result.stats.replaced_semitones == 1  # C#4 tie to C4
    assert result.stats.octave_folds == 2  # C6 to C5 and B2 to B3
    assert [event.keys for event in result.events] == [("A",), ("Q",), ("M",)]

    collision = map_note_sequence(
        _sequence([60, 61], same_time=True),
        MappingConfig(layout=default_mapping_layout(), transpose=0),
    )
    assert [event.keys for event in collision.events] == [("A",)]
    assert collision.stats.collision_notes_removed == 1


def test_same_key_retriggers_are_preserved() -> None:
    sequence = _sequence([60, 60])
    result = map_note_sequence(
        sequence,
        MappingConfig(layout=default_mapping_layout(), transpose=0),
    )
    assert [event.at_us for event in result.events] == [0, 500_000]
    assert [event.keys for event in result.events] == [("A",), ("A",)]
    assert result.stats.collision_notes_removed == 0


def test_auto_transpose_selects_stable_global_shift() -> None:
    sequence = _sequence([61, 63, 65, 66, 68, 70, 72])
    first = map_note_sequence(sequence, MappingConfig(layout=default_mapping_layout()))
    second = map_note_sequence(sequence, MappingConfig(layout=default_mapping_layout()))
    assert first.stats.transpose_semitones == -1
    assert first.stats == second.stats
    assert [note.pitch for note in first.mapped.notes] == [60, 62, 64, 65, 67, 69, 71]


def test_manual_transpose_is_respected() -> None:
    result = map_note_sequence(
        _sequence([60]),
        MappingConfig(layout=default_mapping_layout(), transpose=2),
    )
    assert result.stats.transpose_semitones == 2
    assert result.mapped.notes[0].pitch == 62


def test_transpose_preview_is_complete_and_deterministic() -> None:
    sequence = _sequence([61, 63, 65, 66, 68, 70, 72])
    first = preview_transpositions(sequence)
    second = preview_transpositions(sequence)
    assert first == second
    assert len(first) == 25
    selected = [item for item in first if item.selected]
    assert len(selected) == 1
    assert selected[0].transpose == -1
    assert selected[0].stats.replaced_semitones == 0
    assert [item.score for item in first] == sorted(item.score for item in first)
