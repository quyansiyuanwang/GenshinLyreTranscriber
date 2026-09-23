from __future__ import annotations

from glt_core.domain.note_sequence import BeatGridPoint, Note, NoteSequence, Provenance
from glt_core.processing.quantize import QuantizationConfig, quantize_note_sequence


def _sequence(notes: tuple[Note, ...], *, confidence: float = 1.0) -> NoteSequence:
    beats = tuple(
        BeatGridPoint(index * 500_000, float(index), 120.0, confidence) for index in range(5)
    )
    sequence = NoteSequence(
        duration_us=2_000_000,
        notes=notes,
        tempo_map=(),
        beat_grid=beats,
        provenance=Provenance("audio", 0, "test", {}),
    )
    sequence.validate()
    return sequence


def _note(start_us: int, pitch: int = 60) -> Note:
    return Note(pitch, start_us, start_us + 200_000, 100, None, 0, 0)


def test_auto_chooses_triplet_grid() -> None:
    sequence = _sequence((_note(0), _note(170_000), _note(328_000), _note(500_000)))
    result = quantize_note_sequence(sequence)
    assert result.stats.selected_mode == "triplet"
    assert [note.start_us for note in result.quantized.notes] == [0, 166_667, 333_333, 500_000]
    assert result.stats.max_abs_shift_us <= 6_000


def test_auto_chooses_straight_grid() -> None:
    sequence = _sequence((_note(0), _note(123_000), _note(252_000), _note(375_000)))
    result = quantize_note_sequence(sequence)
    assert result.stats.selected_mode == "straight"
    assert [note.start_us for note in result.quantized.notes] == [0, 125_000, 250_000, 375_000]


def test_preserve_does_not_change_onsets() -> None:
    sequence = _sequence((_note(17_123), _note(188_888)))
    result = quantize_note_sequence(sequence, QuantizationConfig(mode="preserve"))
    assert result.quantized.notes == sequence.notes
    assert result.stats.selected_mode == "preserve"
    assert result.stats.quantized_notes == 0


def test_low_confidence_and_large_shift_fall_back_per_region() -> None:
    low_confidence = _sequence((_note(123_000),), confidence=0.1)
    result = quantize_note_sequence(low_confidence)
    assert result.quantized.notes[0].start_us == 123_000
    assert result.fallback_regions[0].reason == "low_confidence"

    large_shift = _sequence((_note(180_000),))
    result = quantize_note_sequence(
        large_shift,
        QuantizationConfig(mode="straight", max_shift_us=10_000),
    )
    assert result.quantized.notes[0].start_us == 180_000
    assert result.fallback_regions[0].reason == "shift_too_large"


def test_explicit_bpm_and_manual_beats_have_priority() -> None:
    sequence = _sequence((_note(100_000),))
    explicit = quantize_note_sequence(
        sequence,
        QuantizationConfig(mode="straight", bpm=120.0, max_shift_us=30_000),
    )
    assert explicit.quantized.notes[0].start_us == 125_000

    manual = quantize_note_sequence(
        _sequence((_note(140_000),)),
        QuantizationConfig(
            mode="straight",
            manual_beat_times_us=(25_000, 525_000),
            max_shift_us=30_000,
        ),
    )
    assert manual.quantized.notes[0].start_us == 150_000


def test_quantisation_never_creates_invalid_duration_or_order() -> None:
    sequence = _sequence((_note(124_999), _note(125_000, pitch=64)))
    result = quantize_note_sequence(sequence, QuantizationConfig(mode="straight"))
    assert all(note.end_us > note.start_us for note in result.quantized.notes)
    assert [note.start_us for note in result.quantized.notes] == sorted(
        note.start_us for note in result.quantized.notes
    )
