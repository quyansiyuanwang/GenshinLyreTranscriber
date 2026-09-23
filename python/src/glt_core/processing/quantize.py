"""Controllable straight/triplet quantisation with confidence fallback."""

from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import asdict, dataclass, replace
from typing import Literal

from glt_core.domain.note_sequence import Note, NoteSequence

TimingMode = Literal["auto", "preserve", "straight", "triplet"]
SelectedMode = Literal["preserve", "straight", "triplet"]


@dataclass(frozen=True, slots=True)
class QuantizationConfig:
    mode: TimingMode = "auto"
    bpm: float | None = None
    manual_beat_times_us: tuple[int, ...] = ()
    max_shift_us: int = 80_000
    confidence_threshold: float = 0.2

    def validate(self) -> None:
        if self.mode not in {"auto", "preserve", "straight", "triplet"}:
            raise ValueError("unknown quantisation mode")
        if self.bpm is not None and (not math.isfinite(self.bpm) or not 20 <= self.bpm <= 400):
            raise ValueError("bpm must be finite and in range 20..400")
        if any(value < 0 or isinstance(value, bool) for value in self.manual_beat_times_us):
            raise ValueError("manual beat times must be non-negative integers")
        if self.max_shift_us < 0:
            raise ValueError("max_shift_us must be non-negative")
        if not 0 <= self.confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be in range 0..1")


@dataclass(frozen=True, slots=True)
class QuantizationChange:
    note_index: int
    old_start_us: int
    new_start_us: int
    shift_us: int
    reason: str


@dataclass(frozen=True, slots=True)
class FallbackRegion:
    start_us: int
    end_us: int
    reason: str
    confidence: float | None


@dataclass(frozen=True, slots=True)
class QuantizationStats:
    input_notes: int
    quantized_notes: int
    preserved_notes: int
    mean_abs_shift_us: float
    max_abs_shift_us: int
    straight_median_error_us: float | None
    triplet_median_error_us: float | None
    selected_mode: SelectedMode
    fallback_regions: int

    def to_dict(self) -> dict[str, int | float | str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class QuantizationResult:
    original: NoteSequence
    quantized: NoteSequence
    stats: QuantizationStats
    changes: tuple[QuantizationChange, ...]
    fallback_regions: tuple[FallbackRegion, ...]


@dataclass(frozen=True, slots=True)
class _GridPoint:
    at_us: int
    confidence: float


def quantize_note_sequence(
    sequence: NoteSequence,
    config: QuantizationConfig | None = None,
) -> QuantizationResult:
    selected = config or QuantizationConfig()
    selected.validate()
    sequence.validate()
    if selected.mode == "preserve":
        return _result(sequence, sequence, selected, (), (), None, None, "preserve", [])

    straight_grid = _build_grid(sequence, selected, subdivisions=4)
    triplet_grid = _build_grid(sequence, selected, subdivisions=3)
    straight_error = _median_error(sequence.notes, straight_grid)
    triplet_error = _median_error(sequence.notes, triplet_grid)
    chosen = _choose_mode(selected.mode, straight_error, triplet_error)
    if chosen == "preserve":
        fallback = tuple(
            FallbackRegion(
                start_us=note.start_us,
                end_us=note.end_us,
                reason="ambiguous_or_missing_grid",
                confidence=None,
            )
            for note in sequence.notes
        )
        return _result(
            sequence,
            sequence,
            selected,
            (),
            fallback,
            straight_error,
            triplet_error,
            chosen,
        )

    grid = straight_grid if chosen == "straight" else triplet_grid
    if not grid:
        fallback = tuple(
            FallbackRegion(
                start_us=note.start_us,
                end_us=note.end_us,
                reason="missing_grid",
                confidence=None,
            )
            for note in sequence.notes
        )
        return _result(
            sequence,
            sequence,
            selected,
            (),
            fallback,
            straight_error,
            triplet_error,
            "preserve",
        )

    grid_times = [point.at_us for point in grid]
    quantized_notes: list[Note] = []
    changes: list[QuantizationChange] = []
    fallback_regions: list[FallbackRegion] = []
    shifts: list[int] = []
    for index, note in enumerate(sequence.notes):
        grid_index = bisect.bisect_left(grid_times, note.start_us)
        nearest_index = min(
            range(max(0, grid_index - 1), min(len(grid), grid_index + 1)),
            key=lambda candidate: abs(grid[candidate].at_us - note.start_us),
        )
        point = grid[nearest_index]
        shift = point.at_us - note.start_us
        if point.confidence < selected.confidence_threshold or abs(shift) > selected.max_shift_us:
            quantized_notes.append(note)
            fallback_regions.append(
                FallbackRegion(
                    start_us=note.start_us,
                    end_us=note.end_us,
                    reason=(
                        "low_confidence"
                        if point.confidence < selected.confidence_threshold
                        else "shift_too_large"
                    ),
                    confidence=point.confidence,
                )
            )
            continue
        new_start = point.at_us
        new_end = max(new_start + 1, note.end_us + shift)
        quantized_notes.append(replace(note, start_us=new_start, end_us=new_end))
        shifts.append(shift)
        if shift:
            changes.append(
                QuantizationChange(
                    note_index=index,
                    old_start_us=note.start_us,
                    new_start_us=new_start,
                    shift_us=shift,
                    reason=f"{chosen}_grid",
                )
            )
    quantized_notes.sort(
        key=lambda note: (note.start_us, note.pitch, note.track, note.channel, note.end_us)
    )
    parameters = dict(sequence.provenance.parameters)
    parameters["quantization"] = {
        **asdict(selected),
        "selected_mode": chosen,
        "straight_median_error_us": straight_error,
        "triplet_median_error_us": triplet_error,
    }
    quantized = replace(
        sequence,
        notes=tuple(quantized_notes),
        provenance=replace(sequence.provenance, parameters=parameters),
    )
    quantized.validate()
    return _result(
        sequence,
        quantized,
        selected,
        tuple(changes),
        tuple(fallback_regions),
        straight_error,
        triplet_error,
        chosen,
        shifts,
    )


def _build_grid(
    sequence: NoteSequence,
    config: QuantizationConfig,
    *,
    subdivisions: int,
) -> tuple[_GridPoint, ...]:
    if config.manual_beat_times_us:
        manual_beats = sorted(set(int(value) for value in config.manual_beat_times_us))
        manual_grid_points = _grid_from_beats(manual_beats, subdivisions, sequence.duration_us)
        return tuple(_GridPoint(at_us=value, confidence=1.0) for value in manual_grid_points)
    if config.bpm is not None:
        interval = 60_000_000.0 / config.bpm / subdivisions
        return tuple(
            _GridPoint(at_us=round(index * interval), confidence=1.0)
            for index in range(_grid_count(sequence.duration_us, interval))
        )
    if len(sequence.beat_grid) >= 2:
        beat_grid_points: list[int] = []
        beats = sorted(sequence.beat_grid, key=lambda point: point.at_us)
        confidences: list[float] = []
        for index, beat in enumerate(beats):
            next_time = beats[index + 1].at_us if index + 1 < len(beats) else sequence.duration_us
            interval = (next_time - beat.at_us) / subdivisions
            if interval <= 0:
                continue
            local_confidence = beat.confidence if beat.confidence is not None else 0.5
            for step in range(subdivisions):
                at_us = round(beat.at_us + step * interval)
                if at_us > sequence.duration_us:
                    break
                beat_grid_points.append(at_us)
                confidences.append(local_confidence)
        if beat_grid_points:
            beat_grid_points.append(sequence.duration_us)
            confidences.append(confidences[-1])
        return tuple(
            _GridPoint(at_us=at_us, confidence=confidence)
            for at_us, confidence in zip(beat_grid_points, confidences, strict=True)
        )
    if sequence.tempo_map:
        tempo_grid_points: list[int] = []
        tempos = sorted(sequence.tempo_map, key=lambda point: point.at_us)
        for index, tempo in enumerate(tempos):
            next_time = tempos[index + 1].at_us if index + 1 < len(tempos) else sequence.duration_us
            interval = 60_000_000.0 / tempo.bpm / subdivisions
            cursor = float(tempo.at_us)
            while cursor < next_time and cursor <= sequence.duration_us:
                tempo_grid_points.append(round(cursor))
                cursor += interval
        if tempo_grid_points and tempo_grid_points[-1] != sequence.duration_us:
            tempo_grid_points.append(sequence.duration_us)
        return tuple(
            _GridPoint(at_us=value, confidence=0.5) for value in sorted(set(tempo_grid_points))
        )
    return ()


def _grid_from_beats(beats: list[int], subdivisions: int, duration_us: int) -> list[int]:
    points: list[int] = []
    for index, beat in enumerate(beats):
        next_time = beats[index + 1] if index + 1 < len(beats) else duration_us
        interval = (next_time - beat) / subdivisions
        if interval <= 0:
            continue
        for step in range(subdivisions):
            value = round(beat + step * interval)
            if value <= duration_us:
                points.append(value)
    if beats and beats[-1] <= duration_us:
        points.append(beats[-1])
    if not points or points[-1] != duration_us:
        points.append(duration_us)
    return sorted(set(points))


def _grid_count(duration_us: int, interval: float) -> int:
    return max(0, math.floor(duration_us / interval) + 1)


def _median_error(notes: tuple[Note, ...], grid: tuple[_GridPoint, ...]) -> float | None:
    if not notes or not grid:
        return None
    times = [point.at_us for point in grid]
    errors: list[int] = []
    for note in notes:
        index = bisect.bisect_left(times, note.start_us)
        candidates = range(max(0, index - 1), min(len(grid), index + 1))
        errors.append(min(abs(grid[candidate].at_us - note.start_us) for candidate in candidates))
    return float(statistics.median(errors))


def _choose_mode(
    requested: TimingMode,
    straight_error: float | None,
    triplet_error: float | None,
) -> SelectedMode:
    if requested in {"straight", "triplet"}:
        return requested
    if straight_error is None or triplet_error is None:
        return "preserve"
    smaller = min(straight_error, triplet_error)
    larger = max(straight_error, triplet_error)
    if larger <= 1.0:
        return "preserve"
    if smaller <= 0.75 * larger:
        return "straight" if straight_error < triplet_error else "triplet"
    return "preserve"


def _result(
    original: NoteSequence,
    quantized: NoteSequence,
    config: QuantizationConfig,
    changes: tuple[QuantizationChange, ...],
    fallback_regions: tuple[FallbackRegion, ...],
    straight_error: float | None,
    triplet_error: float | None,
    selected_mode: SelectedMode,
    shifts: list[int] | None = None,
) -> QuantizationResult:
    shift_values = shifts or [change.shift_us for change in changes]
    stats = QuantizationStats(
        input_notes=len(original.notes),
        quantized_notes=len(changes),
        preserved_notes=len(original.notes) - len(changes),
        mean_abs_shift_us=(
            float(statistics.fmean(abs(value) for value in shift_values)) if shift_values else 0.0
        ),
        max_abs_shift_us=max((abs(value) for value in shift_values), default=0),
        straight_median_error_us=straight_error,
        triplet_median_error_us=triplet_error,
        selected_mode=selected_mode,
        fallback_regions=len(fallback_regions),
    )
    return QuantizationResult(
        original=original,
        quantized=quantized,
        stats=stats,
        changes=changes,
        fallback_regions=fallback_regions,
    )
