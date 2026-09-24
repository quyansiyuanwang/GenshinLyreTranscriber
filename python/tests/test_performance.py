from __future__ import annotations

import pathlib

import pytest

from glt_core.analysis import analysis_notes_from_basic_pitch
from glt_core.domain.note_sequence import Note, NoteSequence, Provenance
from glt_core.performance import (
    PERFORMANCE_NAME,
    build_performance,
    performance_from_dict,
    performance_to_events_document,
    performance_to_note_sequence,
    read_performance,
    render_performance_bundle,
    write_performance,
)
from glt_core.processing.mapping import MappingResult, map_note_sequence
from glt_core.protocol.validation import validate_events, validate_note_sequence


def _mapping() -> MappingResult:
    sequence = NoteSequence(
        duration_us=1_000_000,
        notes=(
            Note(60, 0, 500_000, 90, 0.8, 0, 0),
            Note(64, 500_000, 900_000, 70, 0.6, 0, 0),
        ),
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance("audio", 0, "test", {}),
    )
    return map_note_sequence(sequence)


def test_performance_round_trip_and_legacy_derivations() -> None:
    analysis = analysis_notes_from_basic_pitch(
        ((0.0, 0.5, 60, 0.75, [0, 2048, 4096]),),
        1_000_000,
    )
    performance = build_performance(
        _mapping(),
        source_type="audio",
        revision_source="transcribe",
        source_offset_us=0,
        analysis_notes=analysis.notes,
    )
    document = performance.to_dict()
    reparsed = performance_from_dict(document)
    assert reparsed == performance
    assert reparsed.notes[0].pitch_bend_class == "slide"
    assert reparsed.notes[0].candidate_id == analysis.notes[0].id

    sequence = performance_to_note_sequence(reparsed)
    validate_note_sequence(sequence.to_dict())
    events = performance_to_events_document(reparsed, generator="test")
    validate_events(events)
    assert [event["keys"] for event in events["events"]] == [["A"], ["D"]]


def test_performance_derived_exports_are_byte_stable(tmp_path: pathlib.Path) -> None:
    performance = build_performance(
        _mapping(),
        source_type="audio",
        revision_source="refilter",
        source_offset_us=0,
    )
    first = render_performance_bundle(
        performance,
        tmp_path / "first",
        title="fixture.wav",
        timing_mode="auto",
        preview_wav=True,
        generator="test",
    )
    second = render_performance_bundle(
        performance,
        tmp_path / "second",
        title="fixture.wav",
        timing_mode="auto",
        preview_wav=True,
        generator="test",
    )
    for first_path, second_path in zip(
        (
            first.performance_path,
            first.midi_path,
            first.events_path,
            first.readable_path,
            first.compatibility_path,
            first.preview_path,
        ),
        (
            second.performance_path,
            second.midi_path,
            second.events_path,
            second.readable_path,
            second.compatibility_path,
            second.preview_path,
        ),
        strict=True,
    ):
        assert first_path is not None and second_path is not None
        assert first_path.read_bytes() == second_path.read_bytes()


def test_performance_write_validates_before_replacing(tmp_path: pathlib.Path) -> None:
    destination = tmp_path / PERFORMANCE_NAME
    destination.write_text("sentinel", encoding="utf-8")
    invalid = build_performance(
        _mapping(),
        source_type="audio",
        revision_source="render_performance",
        source_offset_us=0,
    ).to_dict()
    invalid["notes"][0]["pitch"] = 61
    with pytest.raises(ValueError):
        write_performance(performance_from_dict(invalid), destination)
    assert destination.read_text(encoding="utf-8") == "sentinel"


def test_read_performance_rejects_non_performance_result(tmp_path: pathlib.Path) -> None:
    path = tmp_path / PERFORMANCE_NAME
    path.write_text('{"format_version":1,"events":[]}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_performance(path)
