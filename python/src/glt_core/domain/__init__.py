"""Core domain types shared by transcription and processing stages."""

from glt_core.domain.midi_import import (
    MidiImportError,
    MidiImportResult,
    MidiWarning,
    copy_source_midi,
    import_midi,
)
from glt_core.domain.note_sequence import (
    BeatGridPoint,
    Note,
    NoteSequence,
    Provenance,
    TempoPoint,
)

__all__ = [
    "BeatGridPoint",
    "MidiImportError",
    "MidiImportResult",
    "MidiWarning",
    "Note",
    "NoteSequence",
    "Provenance",
    "TempoPoint",
    "copy_source_midi",
    "import_midi",
]
