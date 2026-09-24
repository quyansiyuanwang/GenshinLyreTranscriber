"""Deterministic stem and performance routing."""

from __future__ import annotations

import json
import math
import os
import pathlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np
import soundfile

from glt_core.domain.note_sequence import BeatGridPoint, Note, NoteSequence, Provenance, TempoPoint
from glt_core.separation.component import STEM_ROLES

TargetRole = Literal["melody", "harmony", "bass", "percussion", "ignore"]
PerformanceMode = Literal["solo", "melody_chords", "two_voice", "full", "custom"]
TARGET_ROLES = {"melody", "harmony", "bass", "percussion", "ignore"}
PERFORMANCE_MODES = {"solo", "melody_chords", "two_voice", "full", "custom"}


@dataclass(frozen=True, slots=True)
class StemRoute:
    id: str
    enabled: bool
    sources: tuple[str, ...]
    target: TargetRole
    gain_db: float = 0.0
    muted: bool = False
    solo: bool = False
    priority: int = 50
    start_us: int | None = None
    end_us: int | None = None

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["sources"] = list(self.sources)
        return document


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    format_version: int
    mode: PerformanceMode
    routes: tuple[StemRoute, ...]
    max_voices: int = 2
    collision: Literal["priority", "highest_velocity", "first"] = "priority"

    def validate(self) -> None:
        if self.format_version != 1:
            raise ValueError("unsupported routing plan format")
        if self.mode not in PERFORMANCE_MODES:
            raise ValueError("invalid performance mode")
        if not 1 <= self.max_voices <= 21:
            raise ValueError("max_voices must be in range 1..21")
        if self.collision not in {"priority", "highest_velocity", "first"}:
            raise ValueError("invalid collision policy")
        identifiers: set[str] = set()
        has_solo = any(route.solo and route.enabled and not route.muted for route in self.routes)
        for route in self.routes:
            if not route.id or route.id in identifiers:
                raise ValueError(f"duplicate or empty route id: {route.id!r}")
            identifiers.add(route.id)
            if not route.sources or any(source not in STEM_ROLES for source in route.sources):
                raise ValueError(f"route {route.id} contains invalid stem sources")
            if route.target not in TARGET_ROLES:
                raise ValueError(f"route {route.id} has invalid target")
            if not math.isfinite(route.gain_db) or not -24.0 <= route.gain_db <= 24.0:
                raise ValueError(f"route {route.id} gain_db must be in -24..24")
            if not 0 <= route.priority <= 100:
                raise ValueError(f"route {route.id} priority must be in 0..100")
            if route.start_us is not None and route.start_us < 0:
                raise ValueError(f"route {route.id} start_us is invalid")
            if route.end_us is not None and route.end_us <= (route.start_us or 0):
                raise ValueError(f"route {route.id} end_us is invalid")
            if has_solo and not route.solo:
                continue

    def active_routes(self) -> tuple[StemRoute, ...]:
        self.validate()
        solo = any(route.solo and route.enabled and not route.muted for route in self.routes)
        return tuple(
            route
            for route in sorted(self.routes, key=lambda value: (-value.priority, value.id))
            if route.enabled and not route.muted and (not solo or route.solo)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "mode": self.mode,
            "max_voices": self.max_voices,
            "collision": self.collision,
            "routes": [route.to_dict() for route in self.routes],
        }


def routing_plan_from_template(
    mode: PerformanceMode,
    *,
    max_voices: int | None = None,
) -> RoutingPlan:
    routes: tuple[StemRoute, ...]
    if mode == "solo":
        routes = (
            StemRoute("melody", True, ("other",), "melody", priority=90),
            StemRoute("optional-vocal", True, ("vocals",), "melody", priority=80, gain_db=-2.0),
        )
        cap = max_voices or 1
    elif mode == "melody_chords":
        routes = (
            StemRoute("melody", True, ("vocals", "other"), "melody", priority=90),
            StemRoute("harmony", True, ("other",), "harmony", priority=70, gain_db=-3.0),
            StemRoute("bass", True, ("bass",), "bass", priority=50, gain_db=-6.0),
        )
        cap = max_voices or 3
    elif mode == "two_voice":
        routes = (
            StemRoute("melody", True, ("vocals", "other"), "melody", priority=90),
            StemRoute("bass", True, ("bass",), "bass", priority=60, gain_db=-4.0),
        )
        cap = max_voices or 2
    elif mode == "full":
        routes = (
            StemRoute("vocals", True, ("vocals",), "melody", priority=90),
            StemRoute("other", True, ("other",), "harmony", priority=70),
            StemRoute("bass", True, ("bass",), "bass", priority=60, gain_db=-3.0),
            StemRoute("drums", False, ("drums",), "percussion", priority=20),
        )
        cap = max_voices or 2
    elif mode == "custom":
        routes = ()
        cap = max_voices or 2
    else:
        raise ValueError(f"unsupported performance mode: {mode}")
    plan = RoutingPlan(1, mode, routes, cap)
    plan.validate()
    return plan


def route_stem_audio(
    stem_paths: Mapping[str, pathlib.Path | str],
    routing: RoutingPlan,
    destination: pathlib.Path | str,
    *,
    duration_us: int | None = None,
) -> pathlib.Path:
    routing.validate()
    output = pathlib.Path(destination).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, np.ndarray] = {}
    sample_rate: int | None = None
    for role in STEM_ROLES:
        source = stem_paths.get(role)
        if source is None:
            continue
        samples, current_rate = soundfile.read(source, dtype="float32", always_2d=True)
        if sample_rate is None:
            sample_rate = current_rate
        elif sample_rate != current_rate:
            raise ValueError("stem sample rates do not match")
        arrays[role] = samples
    if sample_rate is None:
        raise ValueError("no stems were provided")
    length = max(array.shape[0] for array in arrays.values())
    channels = max(array.shape[1] for array in arrays.values())
    mixed = np.zeros((length, channels), dtype=np.float32)
    active = routing.active_routes()
    for route in active:
        if route.target == "ignore":
            continue
        route_mix = np.zeros_like(mixed)
        for role in route.sources:
            array = arrays.get(role)
            if array is None:
                continue
            start = 0 if route.start_us is None else round(route.start_us * sample_rate / 1_000_000)
            end = length if route.end_us is None else round(route.end_us * sample_rate / 1_000_000)
            start = max(0, min(start, length))
            end = max(start, min(end, length))
            if end <= start:
                continue
            source_start = start if start < array.shape[0] else array.shape[0]
            source_end = min(end, array.shape[0])
            if source_end <= source_start:
                continue
            route_mix[start:source_end, : array.shape[1]] += array[source_start:source_end]
        mixed += route_mix * (10.0 ** (route.gain_db / 20.0))
    if duration_us is not None:
        target_frames = round(duration_us * sample_rate / 1_000_000)
        if target_frames < mixed.shape[0]:
            mixed = mixed[:target_frames]
        elif target_frames > mixed.shape[0]:
            mixed = np.pad(mixed, ((0, target_frames - mixed.shape[0]), (0, 0)))
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 0.98:
        mixed *= 0.98 / peak
    partial = output.with_name(f".{output.name}.partial{output.suffix}")
    partial.unlink(missing_ok=True)
    try:
        soundfile.write(partial, mixed, sample_rate, subtype="PCM_24")
        os.replace(partial, output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return output


def merge_routed_sequences(
    sequences: Mapping[str, NoteSequence],
    routing: RoutingPlan,
) -> NoteSequence:
    """Merge route candidates deterministically while enforcing the voice cap."""
    routing.validate()
    active = routing.active_routes()
    if not active:
        raise ValueError("routing plan has no active routes")
    duration_us = max(
        (sequences[route.id].duration_us for route in active if route.id in sequences),
        default=0,
    )
    tempo_map: tuple[TempoPoint, ...] = ()
    beat_grid: tuple[BeatGridPoint, ...] = ()
    provenance: Provenance | None = None
    candidates: list[tuple[int, Note, str]] = []
    for route in active:
        sequence = sequences.get(route.id)
        if sequence is None or route.target == "ignore":
            continue
        if provenance is None:
            provenance = sequence.provenance
            tempo_map = sequence.tempo_map
            beat_grid = sequence.beat_grid
        for note in sequence.notes:
            candidates.append((route.priority, note, route.id))
    if provenance is None:
        raise ValueError("no sequence data matched the routing plan")

    grouped: dict[int, list[tuple[int, Note, str]]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate[1].start_us, []).append(candidate)
    merged: list[Note] = []
    seen: set[tuple[int, int]] = set()
    for start_us in sorted(grouped):
        ordered = sorted(
            grouped[start_us],
            key=lambda value: (-value[0], -value[1].velocity, value[1].pitch, value[2]),
        )
        kept = 0
        for _priority, note, _route_id in ordered:
            identity = (note.start_us, note.pitch)
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(note)
            kept += 1
            if kept >= routing.max_voices:
                break
    sequence = NoteSequence(
        duration_us=duration_us,
        notes=tuple(sorted(merged, key=lambda note: (note.start_us, note.pitch, note.track))),
        tempo_map=tempo_map,
        beat_grid=beat_grid,
        provenance=Provenance(
            source_type=provenance.source_type,
            source_offset_us=provenance.source_offset_us,
            model_version=provenance.model_version,
            parameters={**provenance.parameters, "routing": routing.to_dict()},
        ),
    )
    sequence.validate()
    return sequence


def load_routing_plan(document: dict[str, Any]) -> RoutingPlan:
    if not isinstance(document, dict) or document.get("format_version") != 1:
        raise ValueError("unsupported routing plan")
    routes_document = document.get("routes")
    if not isinstance(routes_document, list):
        raise ValueError("routing routes must be an array")
    routes = tuple(
        StemRoute(
            id=str(route["id"]),
            enabled=bool(route.get("enabled", True)),
            sources=tuple(str(value) for value in route["sources"]),
            target=route["target"],
            gain_db=float(route.get("gain_db", 0.0)),
            muted=bool(route.get("muted", False)),
            solo=bool(route.get("solo", False)),
            priority=int(route.get("priority", 50)),
            start_us=route.get("start_us"),
            end_us=route.get("end_us"),
        )
        for route in routes_document
        if isinstance(route, dict)
    )
    plan = RoutingPlan(
        format_version=1,
        mode=document.get("mode", "custom"),
        routes=routes,
        max_voices=int(document.get("max_voices", 2)),
        collision=document.get("collision", "priority"),
    )
    plan.validate()
    return plan


def write_routing_plan(plan: RoutingPlan, destination: pathlib.Path | str) -> pathlib.Path:
    output = pathlib.Path(destination).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")
    return output
