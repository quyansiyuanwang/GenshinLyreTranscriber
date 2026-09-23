"""Minimal MIDI export for cleaned NoteSequence data."""

from __future__ import annotations

import os
import pathlib

import pretty_midi

from glt_core.domain.note_sequence import NoteSequence


def write_note_sequence_midi(
    sequence: NoteSequence,
    destination: pathlib.Path | str,
    *,
    overwrite: bool = False,
) -> pathlib.Path:
    sequence.validate()
    output = pathlib.Path(destination).expanduser().resolve()
    if output.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial")
    partial.unlink(missing_ok=True)
    midi = pretty_midi.PrettyMIDI(initial_tempo=120.0)
    instrument = pretty_midi.Instrument(program=0, name="GenshinLyreTranscriber")
    midi.instruments.append(instrument)
    for note in sequence.notes:
        instrument.notes.append(
            pretty_midi.Note(
                velocity=note.velocity,
                pitch=note.pitch,
                start=note.start_us / 1_000_000,
                end=note.end_us / 1_000_000,
            )
        )
    try:
        midi.write(str(partial))
        os.replace(partial, output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return output
