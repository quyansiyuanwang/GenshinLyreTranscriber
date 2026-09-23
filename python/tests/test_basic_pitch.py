from __future__ import annotations

import pathlib

import numpy as np
import pytest
import soundfile

from glt_core.transcription.basic_pitch import (
    TranscriptionCancelled,
    _SegmentEvent,
    fuse_segment_events,
    transcribe_to_midi,
)
from glt_core.transcription.onnx_probe import ModelResourceError, resolve_model_path


def test_fuse_segment_events_merges_only_across_segments() -> None:
    events = [
        _SegmentEvent(0.0, 1.0, 60, 0.5, None, 0),
        _SegmentEvent(0.99, 2.0, 60, 0.7, None, 1),
        _SegmentEvent(1.02, 1.4, 60, 0.4, None, 1),
        _SegmentEvent(0.5, 0.8, 64, 0.6, None, 0),
        _SegmentEvent(0.51, 0.9, 64, 0.8, None, 0),
    ]
    fused = fuse_segment_events(events)
    assert [(round(start, 2), round(end, 2), pitch) for start, end, pitch, *_ in fused] == [
        (0.0, 2.0, 60),
        (0.5, 0.8, 64),
        (0.51, 0.9, 64),
        (1.02, 1.4, 60),
    ]


def test_empty_file_returns_valid_empty_midi(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "silence.wav"
    output = tmp_path / "source.mid"
    soundfile.write(source, np.zeros(22_050 * 2, dtype=np.float32), 22_050)
    try:
        resolve_model_path()
    except ModelResourceError:
        pytest.skip("pinned model is not downloaded")

    result = transcribe_to_midi(
        source,
        output,
        segment_seconds=1.0,
        overlap_seconds=0.2,
    )
    assert result.note_count == 0
    assert output.is_file()
    assert output.stat().st_size > 0
    assert not output.with_name(f".{output.name}.partial").exists()


def test_cancellation_removes_partial_output(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "silence.wav"
    output = tmp_path / "cancelled.mid"
    soundfile.write(source, np.zeros(22_050 * 3, dtype=np.float32), 22_050)
    try:
        resolve_model_path()
    except ModelResourceError:
        pytest.skip("pinned model is not downloaded")
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    with pytest.raises(TranscriptionCancelled):
        transcribe_to_midi(
            source,
            output,
            segment_seconds=1.0,
            overlap_seconds=0.2,
            cancelled=cancelled,
        )
    assert not output.exists()
    assert not output.with_name(f".{output.name}.partial").exists()
