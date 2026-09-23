"""Note cleaning, timing analysis and mapping stages."""

from glt_core.processing.clean import (
    CleanConfig,
    CleanResult,
    CleanStats,
    NoteChange,
    clean_note_sequence,
)
from glt_core.processing.quantize import (
    FallbackRegion,
    QuantizationChange,
    QuantizationConfig,
    QuantizationResult,
    QuantizationStats,
    quantize_note_sequence,
)
from glt_core.processing.timing import (
    TimingAnalysis,
    TimingConfig,
    TimingError,
    analyze_timing,
)

__all__ = [
    "CleanConfig",
    "CleanResult",
    "CleanStats",
    "NoteChange",
    "FallbackRegion",
    "QuantizationChange",
    "QuantizationConfig",
    "QuantizationResult",
    "QuantizationStats",
    "TimingAnalysis",
    "TimingConfig",
    "TimingError",
    "analyze_timing",
    "clean_note_sequence",
    "quantize_note_sequence",
]
