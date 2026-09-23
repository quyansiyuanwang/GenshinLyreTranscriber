from __future__ import annotations

import pathlib
from types import SimpleNamespace

import mido
import pytest

from glt_core.domain.midi_import import MidiImportError, copy_source_midi, import_midi
from glt_core.media.ffmpeg import sha256_file


def _save_midi(path: pathlib.Path, midi: mido.MidiFile) -> None:
    midi.save(str(path))


def test_multiple_tempo_changes_are_integrated_exactly(tmp_path: pathlib.Path) -> None:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    conductor = mido.MidiTrack()
    conductor.extend(
        [
            mido.MetaMessage("set_tempo", tempo=500_000, time=0),
            mido.MetaMessage("set_tempo", tempo=1_000_000, time=480),
            mido.MetaMessage("set_tempo", tempo=666_667, time=480),
        ]
    )
    notes = mido.MidiTrack()
    notes.extend(
        [
            mido.Message("note_on", note=60, velocity=100, time=0),
            mido.Message("note_off", note=60, velocity=0, time=480),
            mido.Message("note_on", note=62, velocity=100, time=0),
            mido.Message("note_off", note=62, velocity=0, time=480),
            mido.Message("note_on", note=64, velocity=100, time=0),
            mido.Message("note_off", note=64, velocity=0, time=480),
        ]
    )
    midi.tracks.extend([conductor, notes])
    path = tmp_path / "tempo.mid"
    _save_midi(path, midi)
    source_hash = sha256_file(path)

    result = import_midi(path)
    assert [note.start_us for note in result.sequence.notes] == [0, 500_000, 1_500_000]
    assert [note.end_us for note in result.sequence.notes] == [500_000, 1_500_000, 2_166_667]
    assert result.sequence.duration_us == 2_166_667
    assert [point.at_us for point in result.sequence.tempo_map] == [0, 500_000, 1_500_000]
    assert sha256_file(path) == source_hash


def test_velocity_zero_and_overlapping_same_pitch_use_fifo(tmp_path: pathlib.Path) -> None:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    track = mido.MidiTrack()
    track.extend(
        [
            mido.Message("note_on", note=60, velocity=100, time=0),
            mido.Message("note_on", note=60, velocity=80, time=120),
            mido.Message("note_on", note=60, velocity=0, time=360),
            mido.Message("note_off", note=60, velocity=0, time=120),
        ]
    )
    midi.tracks.append(track)
    path = tmp_path / "overlap.mid"
    _save_midi(path, midi)

    result = import_midi(path)
    assert [(note.start_us, note.end_us, note.velocity) for note in result.sequence.notes] == [
        (0, 500_000, 100),
        (125_000, 625_000, 80),
    ]


def test_format_zero_and_track_channel_semantics(tmp_path: pathlib.Path) -> None:
    format_zero = mido.MidiFile(type=0, ticks_per_beat=480)
    zero_track = mido.MidiTrack(
        [
            mido.Message("note_on", note=60, velocity=100, channel=0, time=0),
            mido.Message("note_off", note=60, velocity=0, channel=0, time=480),
        ]
    )
    format_zero.tracks.append(zero_track)
    zero_path = tmp_path / "format-zero.mid"
    _save_midi(zero_path, format_zero)
    assert import_midi(zero_path).sequence.notes[0].track == 0

    format_one = mido.MidiFile(type=1, ticks_per_beat=480)
    first = mido.MidiTrack(
        [
            mido.Message("note_on", note=60, velocity=100, channel=0, time=0),
            mido.Message("note_off", note=60, velocity=0, channel=0, time=480),
        ]
    )
    second = mido.MidiTrack(
        [
            mido.Message("note_on", note=64, velocity=90, channel=2, time=0),
            mido.Message("note_off", note=64, velocity=0, channel=2, time=480),
        ]
    )
    format_one.tracks.extend([first, second])
    one_path = tmp_path / "format-one.mid"
    _save_midi(one_path, format_one)
    notes = import_midi(one_path).sequence.notes
    assert [(note.track, note.channel) for note in notes] == [(0, 0), (1, 2)]


def test_reports_anomalies_and_filters_percussion(tmp_path: pathlib.Path) -> None:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    track = mido.MidiTrack()
    track.extend(
        [
            mido.Message("note_off", note=62, velocity=0, time=0),
            mido.Message("note_on", note=60, velocity=100, time=0),
            mido.Message("note_on", note=64, velocity=90, channel=9, time=0),
            mido.Message("note_off", note=64, velocity=0, channel=9, time=0),
            mido.Message("control_change", control=1, value=1, time=480),
        ]
    )
    midi.tracks.append(track)
    path = tmp_path / "anomalies.mid"
    _save_midi(path, midi)

    result = import_midi(path)
    warnings = {warning.code: warning.count for warning in result.warnings}
    assert warnings["ORPHAN_NOTE_OFF"] == 1
    assert warnings["MISSING_NOTE_OFF"] == 1
    assert warnings["PERCUSSION_FILTERED"] == 1
    assert len(result.sequence.notes) == 1
    assert result.sequence.notes[0].end_us == 500_000


def test_rejects_format_two_and_smpte(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    format_two = mido.MidiFile(type=2, ticks_per_beat=480)
    format_two.tracks.append(mido.MidiTrack())
    path = tmp_path / "format-two.mid"
    _save_midi(path, format_two)
    with pytest.raises(MidiImportError, match="format 2"):
        import_midi(path)

    smpte_path = tmp_path / "smpte.mid"
    smpte_path.write_bytes(b"placeholder")
    monkeypatch.setattr(
        mido,
        "MidiFile",
        lambda *_args, **_kwargs: SimpleNamespace(type=1, ticks_per_beat=(25, 40), tracks=[]),
    )
    with pytest.raises(MidiImportError, match="SMPTE"):
        import_midi(smpte_path)


def test_copy_source_midi_is_atomic_and_preserves_source(tmp_path: pathlib.Path) -> None:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    midi.tracks.append(mido.MidiTrack())
    source = tmp_path / "source.mid"
    _save_midi(source, midi)
    destination = tmp_path / "output/source.mid"
    before = sha256_file(source)
    copy_source_midi(source, destination)
    assert destination.read_bytes() == source.read_bytes()
    assert sha256_file(source) == before
    with pytest.raises(MidiImportError, match="already exists"):
        copy_source_midi(source, destination)
