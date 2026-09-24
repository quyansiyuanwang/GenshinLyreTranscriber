from __future__ import annotations

import json
import pathlib

import numpy as np
import pytest
import soundfile

from glt_core.analysis import AnalysisCancelled, AnalysisConfig, build_analysis_cache
from glt_core.analysis.decode import ANALYSIS_CHANNELS, ANALYSIS_SAMPLE_RATE
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
