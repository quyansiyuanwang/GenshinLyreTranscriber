from __future__ import annotations

import pathlib

import numpy as np
import pytest
import soundfile

from glt_core.synthesis import SAMPLE_RATE, onset_frame, synthesize_preview_wav


def _document(events: list[dict[str, object]], duration_us: int = 1_000_000) -> dict[str, object]:
    return {
        "format_version": 1,
        "time_unit": "us",
        "duration_us": duration_us,
        "events": events,
        "metadata": {"generator": "test", "mapping_profile": "lyre-21-default"},
    }


def test_onset_frame_rounds_to_nearest_sample() -> None:
    expected = (123_456 * SAMPLE_RATE + 500_000) // 1_000_000
    assert onset_frame(123_456) == expected


def test_preview_onset_is_within_one_sample(tmp_path: pathlib.Path) -> None:
    output = tmp_path / "preview.wav"
    synthesize_preview_wav(_document([{"at_us": 123_456, "keys": ["A"]}]), output)
    samples, sample_rate = soundfile.read(output, dtype="float32")
    assert sample_rate == SAMPLE_RATE
    nonzero = np.flatnonzero(np.abs(samples) > 1e-6)
    assert nonzero.size
    assert abs(int(nonzero[0]) - onset_frame(123_456)) <= 1


def test_chord_is_normalized_without_clipping(tmp_path: pathlib.Path) -> None:
    output = tmp_path / "chord.wav"
    synthesize_preview_wav(
        _document([{"at_us": 0, "keys": ["A", "D", "G", "J"]}]),
        output,
    )
    samples, _sample_rate = soundfile.read(output, dtype="float32")
    assert float(np.max(np.abs(samples))) <= 0.951


def test_preview_preserves_duration_and_voice_tail(tmp_path: pathlib.Path) -> None:
    output = tmp_path / "tail.wav"
    result = synthesize_preview_wav(
        _document([{"at_us": 900_000, "keys": ["A"]}], duration_us=1_000_000),
        output,
    )
    info = soundfile.info(output)
    assert result.frames == info.frames
    assert result.frames > round(1.0 * SAMPLE_RATE)
    assert info.channels == 1
    assert info.subtype == "PCM_16"


def test_empty_score_does_not_create_fake_audio(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="empty event score"):
        synthesize_preview_wav(_document([]), tmp_path / "preview.wav")


def test_preview_requires_explicit_overwrite(tmp_path: pathlib.Path) -> None:
    output = tmp_path / "preview.wav"
    document = _document([{"at_us": 0, "keys": ["A"]}])
    synthesize_preview_wav(document, output)
    with pytest.raises(FileExistsError):
        synthesize_preview_wav(document, output)
