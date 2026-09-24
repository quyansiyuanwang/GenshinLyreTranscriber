from __future__ import annotations

import json
import pathlib

import numpy as np
import pytest
import soundfile

from glt_core.analysis import (
    AnalysisCancelled,
    AnalysisConfig,
    SpectralConfig,
    build_analysis_cache,
)
from glt_core.analysis.decode import ANALYSIS_CHANNELS, ANALYSIS_SAMPLE_RATE
from glt_core.analysis_cli import main as analysis_main
from glt_core.media.ffmpeg import MediaError
from glt_core.protocol.validation import validate_analysis_manifest


def _write_stereo_wave(path: pathlib.Path, seconds: float = 1.0) -> None:
    frame_count = round(seconds * ANALYSIS_SAMPLE_RATE)
    time = np.arange(frame_count, dtype=np.float32) / ANALYSIS_SAMPLE_RATE
    tone = 0.5 * np.sin(2 * np.pi * 440.0 * time, dtype=np.float32)
    stereo = np.column_stack((tone, tone * 0.5)).astype(np.float32)
    soundfile.write(path, stereo, ANALYSIS_SAMPLE_RATE, subtype="FLOAT")


def test_analysis_cache_decodes_waveform_and_reuses_manifest(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "tone.wav"
    _write_stereo_wave(source)
    cache_dir = tmp_path / "analysis"
    config = AnalysisConfig(waveform_levels=(64, 256))

    first = build_analysis_cache(source, cache_dir, config=config)
    assert not first.cache_hit
    assert first.decoded.frames == ANALYSIS_SAMPLE_RATE
    assert first.decoded.channels == ANALYSIS_CHANNELS
    assert first.decoded.duration_us == 1_000_000
    assert first.decoded.path.stat().st_size == ANALYSIS_SAMPLE_RATE * ANALYSIS_CHANNELS * 4
    assert len(first.waveform.levels) == 2

    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["format_version"] == 1
    assert manifest["cache_key"] == first.cache_key
    assert manifest["decode"]["sample_rate"] == ANALYSIS_SAMPLE_RATE
    assert {entry["kind"] for entry in manifest["files"]} == {"pcm", "waveform"}
    validate_analysis_manifest(manifest)

    second = build_analysis_cache(source, cache_dir, config=config)
    assert second.cache_hit
    assert second.cache_key == first.cache_key
    assert second.decoded.sha256 == first.decoded.sha256


def test_analysis_cache_key_changes_for_segment(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "tone.wav"
    _write_stereo_wave(source, seconds=2.0)
    cache_dir = tmp_path / "analysis"

    full = build_analysis_cache(source, cache_dir)
    segment = build_analysis_cache(
        source,
        cache_dir,
        config=AnalysisConfig(start_us=0, end_us=500_000),
    )

    assert not segment.cache_hit
    assert segment.cache_key != full.cache_key
    assert 0.4 <= segment.decoded.duration_us / 1_000_000 <= 0.6


def test_analysis_cancellation_is_explicit(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "tone.wav"
    _write_stereo_wave(source)

    with pytest.raises(AnalysisCancelled):
        build_analysis_cache(source, tmp_path / "analysis", cancelled=lambda: True)


def test_spectral_features_find_known_tone(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "tone.wav"
    _write_stereo_wave(source)
    config = SpectralConfig(fft_size=512, hop_size=128, window="hann")

    result = build_analysis_cache(
        source,
        tmp_path / "analysis",
        spectral_config=config,
    )
    assert result.spectral is not None
    assert result.spectral.spectrogram.frames > 300
    assert result.spectral.spectrogram.bins == 257
    assert result.spectral.spectrogram.path.stat().st_size == (
        result.spectral.spectrogram.frames * result.spectral.spectrogram.bins
    )
    features = np.memmap(
        result.spectral.features.path,
        dtype="<f4",
        mode="r",
        shape=(result.spectral.features.frames, len(result.spectral.features.columns)),
    )
    feature_index = {name: index for index, name in enumerate(result.spectral.features.columns)}
    assert float(np.median(features[:, feature_index["centroid_hz"]])) == pytest.approx(
        440.0,
        abs=15.0,
    )
    assert float(np.median(features[:, feature_index["pitch_hz"]])) == pytest.approx(
        440.0,
        abs=8.0,
    )
    assert float(np.median(features[:, feature_index["pitch_confidence"]])) > 0.7

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    validate_analysis_manifest(manifest)
    assert manifest["spectral"]["window"] == "hann"
    assert {entry["kind"] for entry in manifest["files"]} == {
        "pcm",
        "waveform",
        "spectrogram",
        "features",
    }


def test_chirp_centroid_increases_and_window_variants_are_valid(
    tmp_path: pathlib.Path,
) -> None:
    sample_rate = ANALYSIS_SAMPLE_RATE
    frames = sample_rate * 2
    time = np.arange(frames, dtype=np.float32) / sample_rate
    sweep = np.sin(2 * np.pi * (200.0 * time + 450.0 * time * time), dtype=np.float32)
    source = tmp_path / "chirp.wav"
    soundfile.write(source, np.column_stack((sweep, sweep)), sample_rate, subtype="FLOAT")

    result = build_analysis_cache(
        source,
        tmp_path / "analysis",
        spectral_config=SpectralConfig(fft_size=512, hop_size=128, window="blackman"),
    )
    assert result.spectral is not None
    features = np.memmap(
        result.spectral.features.path,
        dtype="<f4",
        mode="r",
        shape=(result.spectral.features.frames, len(result.spectral.features.columns)),
    )
    centroid_index = result.spectral.features.columns.index("centroid_hz")
    centroid = np.asarray(features[:, centroid_index])
    assert float(np.median(centroid[-20:])) > float(np.median(centroid[:20])) + 300.0

    with pytest.raises(ValueError, match="hop_size"):
        SpectralConfig(fft_size=512, hop_size=600).validate()


def test_wave_fallback_without_ffmpeg(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "tone.wav"
    _write_stereo_wave(source, seconds=0.25)

    def missing_ffmpeg() -> object:
        raise MediaError("FFMPEG_NOT_FOUND", "not installed")

    monkeypatch.setattr("glt_core.analysis.decode.resolve_ffmpeg_tools", missing_ffmpeg)
    result = build_analysis_cache(
        source,
        tmp_path / "analysis",
        spectral_config=SpectralConfig(fft_size=512, hop_size=128),
    )
    assert result.decoded.channels == ANALYSIS_CHANNELS
    assert result.spectral is not None


def test_analysis_cli_emits_progress_and_result(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "tone.wav"
    _write_stereo_wave(source, seconds=0.25)
    exit_code = analysis_main(
        [
            "--input",
            str(source),
            "--output",
            str(tmp_path / "analysis"),
            "--fft-size",
            "512",
            "--hop-size",
            "128",
            "--spectral",
        ]
    )
    assert exit_code == 0
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(message["type"] == "progress" for message in messages)
    result = next(message for message in messages if message["type"] == "result")
    assert result["duration_us"] == 250_000
    assert result["spectral"]["fft_size"] == 512
