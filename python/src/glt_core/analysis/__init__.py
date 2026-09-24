"""Audio analysis cache and feature extraction."""

from glt_core.analysis.cache import (
    ANALYSIS_MANIFEST_NAME,
    AnalysisConfig,
    AnalysisResult,
    build_analysis_cache,
)
from glt_core.analysis.decode import AnalysisCancelled
from glt_core.analysis.notes import (
    AnalysisNote,
    AnalysisNoteSet,
    PitchBendPoint,
    analysis_notes_from_basic_pitch,
    classify_pitch_bend,
    estimate_velocity,
    write_analysis_midi,
    write_analysis_notes,
)
from glt_core.analysis.spectrogram import SpectralConfig

__all__ = [
    "ANALYSIS_MANIFEST_NAME",
    "AnalysisCancelled",
    "AnalysisConfig",
    "AnalysisResult",
    "AnalysisNote",
    "AnalysisNoteSet",
    "PitchBendPoint",
    "SpectralConfig",
    "analysis_notes_from_basic_pitch",
    "classify_pitch_bend",
    "estimate_velocity",
    "build_analysis_cache",
    "write_analysis_midi",
    "write_analysis_notes",
]
