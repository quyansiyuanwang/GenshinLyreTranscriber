from __future__ import annotations

import pytest

from glt_core.domain.note_sequence import Note, NoteSequence, Provenance
from glt_core.processing.filtering import (
    FilterRule,
    FilterSpec,
    FilterValidationError,
    FloatRange,
    IntRange,
    apply_filter,
    filter_spec_from_dict,
)


def _note(
    pitch: int,
    start_us: int,
    *,
    duration_us: int = 200_000,
    velocity: int = 80,
    confidence: float | None = 0.7,
) -> Note:
    return Note(pitch, start_us, start_us + duration_us, velocity, confidence, 0, 0)


def _sequence(notes: tuple[Note, ...]) -> NoteSequence:
    sequence = NoteSequence(
        duration_us=max(note.end_us for note in notes),
        notes=notes,
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance("audio", 0, "test", {}),
    )
    sequence.validate()
    return sequence


def test_grouped_rules_use_and_within_rule_and_or_across_rules() -> None:
    sequence = _sequence(
        (
            _note(36, 0, duration_us=50_000, velocity=120, confidence=0.8),
            _note(64, 100_000, duration_us=400_000, velocity=70, confidence=0.5),
            _note(72, 600_000, duration_us=100_000, velocity=90, confidence=0.9),
        )
    )
    spec = FilterSpec(
        rules=(
            FilterRule(
                confidence=FloatRange(0.7, 1.0),
                duration_ms=FloatRange(200.0, 1000.0),
            ),
            FilterRule(
                pitch=IntRange(60, 67),
                velocity=IntRange(60, 80),
            ),
        )
    )
    result = apply_filter(sequence, spec)
    assert [note.pitch for note in result.filtered.notes] == [64]
    assert result.stats.rule_hits == (0, 1)
    assert result.stats.dropped_notes == 2


def test_closed_boundaries_and_disabled_rules() -> None:
    sequence = _sequence((_note(60, 0, duration_us=100_000, velocity=64, confidence=0.5),))
    spec = FilterSpec(
        rules=(
            FilterRule(enabled=False, pitch=IntRange(0, 0)),
            FilterRule(
                confidence=FloatRange(0.5, 0.5),
                duration_ms=FloatRange(100.0, 100.0),
                velocity=IntRange(64, 64),
                pitch=IntRange(60, 60),
            ),
        )
    )
    assert apply_filter(sequence, spec).filtered.notes == sequence.notes


def test_missing_confidence_fails_confidence_condition() -> None:
    sequence = _sequence((_note(60, 0, confidence=None),))
    with_confidence = FilterSpec(
        rules=(FilterRule(confidence=FloatRange(0.0, 1.0)),),
    )
    without_confidence = FilterSpec(rules=(FilterRule(pitch=IntRange(60, 60)),))
    assert not apply_filter(sequence, with_confidence).filtered.notes
    assert apply_filter(sequence, without_confidence).filtered.notes == sequence.notes


def test_empty_rules_keep_all_notes() -> None:
    sequence = _sequence((_note(60, 0), _note(62, 300_000)))
    result = apply_filter(sequence, FilterSpec())
    assert result.filtered.notes == sequence.notes
    assert result.stats.matched_by == ("all",)


@pytest.mark.parametrize(
    "document",
    [
        {"format_version": 2, "rules": []},
        {"format_version": 1, "rules": [{"confidence": {"min": 0.8, "max": 0.2}}]},
        {"format_version": 1, "rules": [{"velocity": {"min": 0, "max": 127}}]},
        {"format_version": 1, "rules": [{"pitch": {"min": 10, "max": 128}}]},
        {"format_version": 1, "rules": [{"unknown": True}]},
    ],
)
def test_invalid_filter_specs_are_rejected(document: dict[str, object]) -> None:
    with pytest.raises(FilterValidationError):
        filter_spec_from_dict(document)
