from __future__ import annotations

import pathlib

import numpy as np
import soundfile

from glt_core.domain.note_sequence import BeatGridPoint, Note, NoteSequence, Provenance, TempoPoint
from glt_core.processing.timing import TimingConfig, analyze_timing


def _note_sequence(click_times: list[float], duration_us: int = 8_000_000) -> NoteSequence:
    notes = tuple(
        Note(
            pitch=60,
            start_us=round(time_value * 1_000_000),
            end_us=round(time_value * 1_000_000) + 80_000,
            velocity=100,
            confidence=0.9,
            track=0,
            channel=0,
        )
        for time_value in click_times
    )
    sequence = NoteSequence(
        duration_us=duration_us,
        notes=notes,
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance("audio", 0, "test", {}),
    )
    sequence.validate()
    return sequence


def _write_click_track(path: pathlib.Path, click_times: list[float], duration: float = 8.0) -> None:
    sample_rate = 22_050
    samples = np.zeros(round(duration * sample_rate), dtype=np.float32)
    click = np.sin(2 * np.pi * 1_000 * np.arange(441) / sample_rate) * np.linspace(1, 0, 441)
    for time_value in click_times:
        start = round(time_value * sample_rate)
        end = min(samples.size, start + click.size)
        samples[start:end] += click[: end - start]
    soundfile.write(path, samples, sample_rate)


def test_regular_click_track_estimates_beats_and_tempo(tmp_path: pathlib.Path) -> None:
    click_times = [index * 0.5 for index in range(16)]
    audio = tmp_path / "clicks.wav"
    _write_click_track(audio, click_times)
    analysis = analyze_timing(audio, _note_sequence(click_times), config=TimingConfig())
    assert not analysis.fallback
    assert analysis.estimated_bpm is not None
    assert 110 <= analysis.estimated_bpm <= 130
    assert analysis.beat_count >= 8
    assert analysis.sequence.tempo_map
    assert analysis.sequence.beat_grid
    assert all(point.source == "estimated" for point in analysis.sequence.tempo_map)


def test_silence_falls_back_without_inventing_tempo(tmp_path: pathlib.Path) -> None:
    audio = tmp_path / "silence.wav"
    soundfile.write(audio, np.zeros(22_050 * 4, dtype=np.float32), 22_050)
    analysis = analyze_timing(audio, _note_sequence([], duration_us=4_000_000))
    assert analysis.fallback
    assert analysis.reason == "no_onsets"
    assert analysis.sequence.beat_grid == ()
    assert analysis.sequence.tempo_map == ()


def test_tempo_ramp_keeps_local_speed_changes(tmp_path: pathlib.Path) -> None:
    click_times: list[float] = [0.0]
    time_value = 0.0
    for bpm in np.linspace(70.0, 110.0, 60):
        time_value += 60.0 / float(bpm)
        click_times.append(time_value)
    audio = tmp_path / "variable.wav"
    _write_click_track(audio, click_times, duration=time_value + 1.0)
    analysis = analyze_timing(
        audio,
        _note_sequence(click_times, duration_us=round((time_value + 1.0) * 1_000_000)),
    )
    local_bpms = [point.bpm for point in analysis.sequence.tempo_map]
    assert not analysis.fallback
    assert max(local_bpms) > 100
    assert min(local_bpms) < 90


def test_midi_tempo_map_is_preserved() -> None:
    sequence = NoteSequence(
        duration_us=2_000_000,
        notes=(Note(60, 0, 500_000, 100, None, 0, 0),),
        tempo_map=(TempoPoint(0, 120.0, "midi"), TempoPoint(500_000, 90.0, "midi")),
        beat_grid=(BeatGridPoint(0, 0.0, 120.0, None),),
        provenance=Provenance("midi", 0, None, {}),
    )
    analysis = analyze_timing(None, sequence)
    assert not analysis.fallback
    assert analysis.reason == "preserve_midi"
    assert analysis.sequence == sequence
    assert analysis.sequence.tempo_map == sequence.tempo_map
    assert analysis.sequence.beat_grid == sequence.beat_grid
