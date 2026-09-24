from __future__ import annotations

import pathlib

from glt_core.domain.midi_import import import_midi
from glt_core.domain.note_sequence import Note, NoteSequence, Provenance
from glt_core.export import build_events_document, write_events_document, write_note_sequence_midi
from glt_core.processing.mapping import MappedEvent
from glt_core.protocol.validation import parse_json_text, validate_events


def _sequence(notes: tuple[Note, ...], duration_us: int = 2_000_000) -> NoteSequence:
    return NoteSequence(
        duration_us=duration_us,
        notes=notes,
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance("audio", 0, "test", {}),
    )


def test_events_json_preserves_exact_microseconds_and_tail(tmp_path: pathlib.Path) -> None:
    sequence = _sequence((), duration_us=1_234_567)
    document = build_events_document(
        sequence,
        (
            MappedEvent(333_333, ("S", "G"), (0, 1)),
            MappedEvent(1_000_000, ("D",), (2,)),
        ),
        generator="GenshinLyreTranscriber 0.1.0",
        mapping_profile="lyre-21-default",
    )
    output = tmp_path / "score.events.json"
    write_events_document(document, output)
    loaded = parse_json_text(output.read_text(encoding="utf-8"))
    validate_events(loaded)
    assert loaded["duration_us"] == 1_234_567
    assert loaded["events"] == [
        {"at_us": 333_333, "keys": ["S", "G"]},
        {"at_us": 1_000_000, "keys": ["D"]},
    ]


def test_events_json_merges_same_onset_in_keyboard_order(tmp_path: pathlib.Path) -> None:
    sequence = _sequence(())
    document = build_events_document(
        sequence,
        (
            MappedEvent(100_000, ("G", "S"), (0, 1)),
            MappedEvent(100_000, ("D", "S"), (2, 3)),
        ),
        generator="test",
        mapping_profile="default",
    )
    output = tmp_path / "merged.events.json"
    write_events_document(document, output)
    loaded = parse_json_text(output.read_text(encoding="utf-8"))
    assert loaded["events"] == [{"at_us": 100_000, "keys": ["S", "D", "G"]}]


def test_empty_events_json_keeps_timed_silence(tmp_path: pathlib.Path) -> None:
    sequence = _sequence((), duration_us=2_500_000)
    document = build_events_document(
        sequence,
        (),
        generator="test",
        mapping_profile="default",
    )
    output = tmp_path / "empty.events.json"
    write_events_document(document, output)
    loaded = parse_json_text(output.read_text(encoding="utf-8"))
    assert loaded["events"] == []
    assert loaded["duration_us"] == 2_500_000


def test_midi_round_trip_onset_error_within_one_millisecond(tmp_path: pathlib.Path) -> None:
    sequence = _sequence(
        (
            Note(60, 0, 200_000, 100, None, 0, 0),
            Note(62, 123_456, 400_000, 100, None, 0, 0),
            Note(64, 987_654, 1_200_000, 100, None, 0, 0),
        )
    )
    midi_path = tmp_path / "mapped.mid"
    write_note_sequence_midi(sequence, midi_path)
    imported = import_midi(midi_path).sequence
    original_starts = [note.start_us for note in sequence.notes]
    imported_starts = [note.start_us for note in imported.notes]
    assert len(imported_starts) == len(original_starts)
    assert (
        max(abs(left - right) for left, right in zip(original_starts, imported_starts, strict=True))
        <= 1_000
    )
