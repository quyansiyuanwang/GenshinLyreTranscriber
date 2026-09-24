"""Versioned grouped note filtering before structural cleaning."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any

from glt_core.domain.note_sequence import Note, NoteSequence

FILTER_FORMAT_VERSION = 1
MAX_FILTER_RULES = 8


class FilterValidationError(ValueError):
    """A stable validation error for filter specifications."""


@dataclass(frozen=True, slots=True)
class FloatRange:
    min: float
    max: float

    def validate(self, name: str, lower: float, upper: float) -> None:
        if (
            not math.isfinite(self.min)
            or not math.isfinite(self.max)
            or not lower <= self.min <= self.max <= upper
        ):
            raise FilterValidationError(f"{name} must be within {lower}..{upper} and min <= max")

    def contains(self, value: float | None) -> bool:
        return value is not None and self.min <= value <= self.max


@dataclass(frozen=True, slots=True)
class IntRange:
    min: int
    max: int

    def validate(self, name: str, lower: int, upper: int) -> None:
        if (
            isinstance(self.min, bool)
            or isinstance(self.max, bool)
            or not lower <= self.min <= self.max <= upper
        ):
            raise FilterValidationError(f"{name} must be within {lower}..{upper} and min <= max")

    def contains(self, value: int | None) -> bool:
        return value is not None and self.min <= value <= self.max


@dataclass(frozen=True, slots=True)
class FilterRule:
    enabled: bool = True
    confidence: FloatRange | None = None
    duration_ms: FloatRange | None = None
    velocity: IntRange | None = None
    pitch: IntRange | None = None

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise FilterValidationError("enabled must be boolean")
        if self.confidence is not None:
            self.confidence.validate("confidence", 0.0, 1.0)
        if self.duration_ms is not None:
            self.duration_ms.validate("duration_ms", 0.0, 3_600_000.0)
        if self.velocity is not None:
            self.velocity.validate("velocity", 1, 127)
        if self.pitch is not None:
            self.pitch.validate("pitch", 0, 127)

    def matches(self, note: Note) -> bool:
        if not self.enabled:
            return False
        if self.confidence is not None and not self.confidence.contains(note.confidence):
            return False
        duration_ms = (note.end_us - note.start_us) / 1_000.0
        if self.duration_ms is not None and not self.duration_ms.contains(duration_ms):
            return False
        if self.velocity is not None and not self.velocity.contains(note.velocity):
            return False
        return self.pitch is None or self.pitch.contains(note.pitch)


@dataclass(frozen=True, slots=True)
class FilterSpec:
    rules: tuple[FilterRule, ...] = ()
    format_version: int = FILTER_FORMAT_VERSION

    def validate(self) -> None:
        if self.format_version != FILTER_FORMAT_VERSION:
            raise FilterValidationError("unsupported filter format version")
        if len(self.rules) > MAX_FILTER_RULES:
            raise FilterValidationError(f"at most {MAX_FILTER_RULES} rules are supported")
        for rule in self.rules:
            rule.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "format_version": self.format_version,
            "rules": [_rule_to_dict(rule) for rule in self.rules],
        }

    def matches(self, note: Note) -> bool:
        enabled = [rule for rule in self.rules if rule.enabled]
        return not enabled or any(rule.matches(note) for rule in enabled)


@dataclass(frozen=True, slots=True)
class FilterStats:
    input_notes: int
    matched_notes: int
    dropped_notes: int
    rule_hits: tuple[int, ...]
    matched_by: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_notes": self.input_notes,
            "matched_notes": self.matched_notes,
            "dropped_notes": self.dropped_notes,
            "rule_hits": list(self.rule_hits),
            "matched_by": list(self.matched_by),
        }


@dataclass(frozen=True, slots=True)
class FilterResult:
    original: NoteSequence
    filtered: NoteSequence
    spec: FilterSpec
    stats: FilterStats


def legacy_filter_spec(*, min_confidence: float, min_duration_us: int) -> FilterSpec:
    """Represent the pre-v2 lower-bound cleaning filters as one v1 rule."""
    spec = FilterSpec(
        rules=(
            FilterRule(
                confidence=FloatRange(float(min_confidence), 1.0),
                duration_ms=FloatRange(min_duration_us / 1_000.0, 3_600_000.0),
            ),
        )
    )
    spec.validate()
    return spec


def apply_filter(sequence: NoteSequence, spec: FilterSpec | None = None) -> FilterResult:
    """Retain notes matching any enabled rule, with all ranges in that rule satisfied."""
    selected = spec or FilterSpec()
    selected.validate()
    sequence.validate()
    retained: list[Note] = []
    rule_hits = [0 for _ in selected.rules]
    matched_by: set[str] = set()
    for note in sequence.notes:
        matches = [index for index, rule in enumerate(selected.rules) if rule.matches(note)]
        if matches:
            retained.append(note)
            for index in matches:
                rule_hits[index] += 1
                matched_by.add(f"rule-{index + 1:02d}")
        elif not any(rule.enabled for rule in selected.rules):
            retained.append(note)
            matched_by.add("all")
    filtered = replace(
        sequence,
        notes=tuple(retained),
        provenance=replace(
            sequence.provenance,
            parameters={
                **sequence.provenance.parameters,
                "filter": selected.to_dict(),
            },
        ),
    )
    filtered.validate()
    return FilterResult(
        original=sequence,
        filtered=filtered,
        spec=selected,
        stats=FilterStats(
            input_notes=len(sequence.notes),
            matched_notes=len(retained),
            dropped_notes=len(sequence.notes) - len(retained),
            rule_hits=tuple(rule_hits),
            matched_by=tuple(sorted(matched_by)),
        ),
    )


def filter_spec_from_dict(document: Any) -> FilterSpec:
    if not isinstance(document, dict):
        raise FilterValidationError("filter specification must be an object")
    unknown = set(document) - {"format_version", "rules"}
    if unknown:
        raise FilterValidationError(f"unknown filter fields: {', '.join(sorted(unknown))}")
    version = document.get("format_version", FILTER_FORMAT_VERSION)
    rules_document = document.get("rules", [])
    if isinstance(version, bool) or not isinstance(version, int):
        raise FilterValidationError("format_version must be an integer")
    if not isinstance(rules_document, list):
        raise FilterValidationError("rules must be an array")
    rules = tuple(_rule_from_dict(rule) for rule in rules_document)
    spec = FilterSpec(rules=rules, format_version=version)
    spec.validate()
    return spec


def _rule_to_dict(rule: FilterRule) -> dict[str, Any]:
    document: dict[str, Any] = {"enabled": rule.enabled}
    if rule.confidence is not None:
        document["confidence"] = asdict(rule.confidence)
    if rule.duration_ms is not None:
        document["duration_ms"] = asdict(rule.duration_ms)
    if rule.velocity is not None:
        document["velocity"] = asdict(rule.velocity)
    if rule.pitch is not None:
        document["pitch"] = asdict(rule.pitch)
    return document


def _rule_from_dict(document: Any) -> FilterRule:
    if not isinstance(document, dict):
        raise FilterValidationError("filter rule must be an object")
    unknown = set(document) - {"enabled", "confidence", "duration_ms", "velocity", "pitch"}
    if unknown:
        raise FilterValidationError(f"unknown rule fields: {', '.join(sorted(unknown))}")
    enabled = document.get("enabled", True)
    if not isinstance(enabled, bool):
        raise FilterValidationError("enabled must be boolean")
    return FilterRule(
        enabled=enabled,
        confidence=_float_range(document.get("confidence"), "confidence"),
        duration_ms=_float_range(document.get("duration_ms"), "duration_ms"),
        velocity=_int_range(document.get("velocity"), "velocity"),
        pitch=_int_range(document.get("pitch"), "pitch"),
    )


def _float_range(document: Any, name: str) -> FloatRange | None:
    if document is None:
        return None
    if not isinstance(document, dict) or set(document) != {"min", "max"}:
        raise FilterValidationError(f"{name} range must contain only min and max")
    minimum = document["min"]
    maximum = document["max"]
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int | float)
        or not isinstance(maximum, int | float)
    ):
        raise FilterValidationError(f"{name} range values must be numbers")
    value = FloatRange(float(minimum), float(maximum))
    value.validate(name, 0.0, 3_600_000.0 if name == "duration_ms" else 1.0)
    return value


def _int_range(document: Any, name: str) -> IntRange | None:
    if document is None:
        return None
    if not isinstance(document, dict) or set(document) != {"min", "max"}:
        raise FilterValidationError(f"{name} range must contain only min and max")
    minimum = document["min"]
    maximum = document["max"]
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int)
        or not isinstance(maximum, int)
    ):
        raise FilterValidationError(f"{name} range values must be integers")
    value = IntRange(minimum, maximum)
    value.validate(name, 1 if name == "velocity" else 0, 127)
    return value
