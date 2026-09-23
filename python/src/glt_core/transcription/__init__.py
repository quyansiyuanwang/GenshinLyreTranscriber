"""Transcription backends and model resource validation."""

from glt_core.transcription.basic_pitch import (
    DEFAULT_OVERLAP_SECONDS,
    DEFAULT_SEGMENT_SECONDS,
    BasicPitchRuntime,
    TranscriptionCancelled,
    TranscriptionError,
    TranscriptionResult,
    fuse_segment_events,
    load_basic_pitch_runtime,
    transcribe_to_midi,
)
from glt_core.transcription.onnx_probe import ModelInfo, ModelResourceError, resolve_model_path

__all__ = [
    "DEFAULT_OVERLAP_SECONDS",
    "DEFAULT_SEGMENT_SECONDS",
    "BasicPitchRuntime",
    "ModelInfo",
    "ModelResourceError",
    "TranscriptionCancelled",
    "TranscriptionError",
    "TranscriptionResult",
    "fuse_segment_events",
    "load_basic_pitch_runtime",
    "resolve_model_path",
    "transcribe_to_midi",
]
