"""Explainable automatic FilterSpec recommendations from candidate notes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from glt_core.domain.note_sequence import NoteSequence
from glt_core.processing.filtering import (
    FilterRule,
    FilterSpec,
    FloatRange,
    IntRange,
    apply_filter,
)

FilterPreset = Literal["off", "auto", "balanced", "melody"]


@dataclass(frozen=True, slots=True)
class AutoFilterRecommendation:
    spec: FilterSpec
    diagnostics: dict[str, Any]


def filter_preset(name: str, sequence: NoteSequence) -> AutoFilterRecommendation:
    selected = name.strip().lower()
    if selected == "off":
        return AutoFilterRecommendation(
            spec=FilterSpec(),
            diagnostics={"preset": "off", "automatic": False},
        )
    if selected == "auto":
        return detect_filter_spec(sequence)
    if selected == "balanced":
        return AutoFilterRecommendation(
            spec=_balanced_spec(),
            diagnostics={"preset": "balanced", "automatic": False},
        )
    if selected == "melody":
        return AutoFilterRecommendation(
            spec=_melody_spec(),
            diagnostics={"preset": "melody", "automatic": False},
        )
    raise ValueError("filter preset must be off, auto, balanced or melody")


def detect_filter_spec(sequence: NoteSequence) -> AutoFilterRecommendation:
    """Recommend OR-of-conditions rules from robust feature quantiles."""
    sequence.validate()
    notes = sequence.notes
    if len(notes) < 20:
        return AutoFilterRecommendation(
            spec=FilterSpec(),
            diagnostics={
                "preset": "auto",
                "automatic": True,
                "fallback": "too_few_notes",
                "input_notes": len(notes),
            },
        )

    durations = sorted((note.end_us - note.start_us) / 1_000.0 for note in notes)
    velocities = sorted(float(note.velocity) for note in notes)
    confidences = sorted(float(note.confidence) for note in notes if note.confidence is not None)
    pitches = sorted(float(note.pitch) for note in notes)
    duration_floor = round(_clamp(_quantile(durations, 0.15), 100.0, 250.0) / 5.0) * 5.0
    confidence_floor = round(
        _clamp(
            _quantile(confidences, 0.30) if confidences else 0.0,
            0.2,
            0.55,
        ),
        2,
    )
    velocity_ceiling = _clamp(_quantile(velocities, 0.75), 50.0, 115.0)
    pitch_floor = _clamp(_quantile(pitches, 0.30), 0.0, 69.0)

    harmonic_rule = FilterRule(pitch=IntRange(math.ceil(pitch_floor), 127))
    if len(confidences) >= len(notes) * 0.8:
        harmonic_rule = FilterRule(
            confidence=FloatRange(confidence_floor, 1.0),
            pitch=IntRange(math.ceil(pitch_floor), 127),
        )
    rules: list[FilterRule] = [
        FilterRule(duration_ms=FloatRange(duration_floor, 3_600_000.0)),
        harmonic_rule,
    ]
    spec = FilterSpec(rules=tuple(rules))
    result = apply_filter(sequence, spec)
    suspect_ratio = result.stats.dropped_notes / len(notes)
    diagnostics = {
        "preset": "auto",
        "automatic": True,
        "input_notes": len(notes),
        "matched_notes": result.stats.matched_notes,
        "suspected_notes": result.stats.dropped_notes,
        "suspect_ratio": suspect_ratio,
        "duration_floor_ms": duration_floor,
        "confidence_floor": confidence_floor,
        "velocity_ceiling": math.floor(velocity_ceiling),
        "pitch_floor": pitch_floor,
        "confidence_coverage": len(confidences) / len(notes),
        "used_confidence": len(confidences) >= len(notes) * 0.8,
        "reason": "retain long notes or notes that are both confident and high enough",
    }
    return AutoFilterRecommendation(spec=spec, diagnostics=diagnostics)


def _balanced_spec() -> FilterSpec:
    return FilterSpec(
        rules=(
            FilterRule(
                duration_ms=FloatRange(140.0, 60_000.0),
                velocity=IntRange(1, 100),
                pitch=IntRange(40, 96),
            ),
            FilterRule(
                duration_ms=FloatRange(300.0, 60_000.0),
                confidence=FloatRange(0.4, 1.0),
            ),
        )
    )


def _melody_spec() -> FilterSpec:
    return FilterSpec(
        rules=(
            FilterRule(
                confidence=FloatRange(0.4, 1.0),
                duration_ms=FloatRange(200.0, 60_000.0),
            ),
            FilterRule(duration_ms=FloatRange(500.0, 60_000.0)),
        )
    )


def _quantile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
