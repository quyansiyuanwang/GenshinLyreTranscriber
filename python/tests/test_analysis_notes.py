from __future__ import annotations

import pathlib

import pretty_midi

from glt_core.analysis import (
    analysis_notes_from_basic_pitch,
    classify_pitch_bend,
    estimate_velocity,
    write_analysis_midi,
    write_analysis_notes,
)
from glt_core.domain.note_sequence import Note, NoteSequence, Provenance
from glt_core.export import write_note_sequence_midi
from glt_core.protocol.validation import validate_analysis_notes


def test_analysis_notes_preserve_pitch_bends_and_classify() -> None:
    stable = analysis_notes_from_basic_pitch(
        ((0.0, 0.5, 60, 0.8, [0, 0, 0]),),
        1_000_000,
        source_stem="other",
    )
    assert stable.notes[0].pitch_bend_class == "stable"
    assert len(stable.notes[0].pitch_bends) == 3
    document = stable.to_dict()
    validate_analysis_notes(document)

    vibrato = analysis_notes_from_basic_pitch(
        ((0.1, 0.6, 67, 0.5, [400, -400, 400, -400]),),
        1_000_000,
    )
    assert vibrato.notes[0].pitch_bend_class == "vibrato"

    slide = analysis_notes_from_basic_pitch(
        ((0.1, 0.7, 64, 0.6, [0, 1024, 2048, 4096]),),
        1_000_000,
    )
    assert slide.notes[0].pitch_bend_class == "slide"
    assert classify_pitch_bend(()) == "stable"


def test_velocity_estimation_is_bounded_and_responsive() -> None:
    quiet = estimate_velocity(0.05, 50_000, source_gain_db=-6.0)
    loud = estimate_velocity(0.9, 800_000, rms_dbfs=-18.0)
    assert 1 <= quiet < loud <= 127
    assert estimate_velocity(-1.0, 100_000) == 1
    assert estimate_velocity(10.0, 100_000) == 127


def test_analysis_midi_keeps_velocity_and_bends_but_mapped_midi_does_not(
    tmp_path: pathlib.Path,
) -> None:
    notes = analysis_notes_from_basic_pitch(
        ((0.0, 0.5, 69, 0.75, [0, 2048, 4096, 2048]),),
        1_000_000,
    )
    notes_path = write_analysis_notes(notes, tmp_path / "notes-v1.json")
    assert notes_path.is_file()
    analysis_midi_path = write_analysis_midi(notes, tmp_path / "analysis.mid")
    analysis_midi = pretty_midi.PrettyMIDI(str(analysis_midi_path))
    assert analysis_midi.resolution == 960
    assert analysis_midi.instruments[0].notes[0].velocity == notes.notes[0].velocity
    assert len(analysis_midi.instruments[0].pitch_bends) == 4

    mapped = NoteSequence(
        duration_us=1_000_000,
        notes=(Note(69, 0, 500_000, 90, 0.8, 0, 0),),
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance("audio", 0, "test", {}),
    )
    mapped_path = write_note_sequence_midi(mapped, tmp_path / "mapped.mid")
    mapped_midi = pretty_midi.PrettyMIDI(str(mapped_path))
    assert not mapped_midi.instruments[0].pitch_bends
