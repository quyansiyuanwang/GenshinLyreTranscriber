"""Note cleaning, timing analysis and mapping stages."""

from glt_core.processing.clean import (
    CleanConfig,
    CleanResult,
    CleanStats,
    NoteChange,
    clean_note_sequence,
)
from glt_core.processing.mapping import (
    DEFAULT_HIGH_PITCH,
    DEFAULT_LOW_PITCH,
    KEYBOARD_ORDER,
    KeyboardKey,
    MappedEvent,
    MappingConfig,
    MappingLayout,
    MappingResult,
    MappingStats,
    TransposePreview,
    default_mapping_layout,
    map_note_sequence,
    preview_transpositions,
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
    "DEFAULT_HIGH_PITCH",
    "DEFAULT_LOW_PITCH",
    "KEYBOARD_ORDER",
    "KeyboardKey",
    "MappedEvent",
    "MappingConfig",
    "MappingLayout",
    "MappingResult",
    "MappingStats",
    "TransposePreview",
    "TimingAnalysis",
    "TimingConfig",
    "TimingError",
    "analyze_timing",
    "clean_note_sequence",
    "default_mapping_layout",
    "map_note_sequence",
    "preview_transpositions",
    "quantize_note_sequence",
]
