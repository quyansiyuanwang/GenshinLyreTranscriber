from __future__ import annotations

import pathlib

import numpy as np
import pytest
import soundfile

from glt_core.domain.note_sequence import Note, NoteSequence, Provenance
from glt_core.processing import (
    RoutingPlan,
    StemRoute,
    load_routing_plan,
    merge_routed_sequences,
    route_stem_audio,
    routing_plan_from_template,
)
from glt_core.protocol.validation import validate_routing_plan


def _stem(path: pathlib.Path, amplitude: float) -> None:
    sample_rate = 44_100
    time = np.arange(sample_rate // 10, dtype=np.float32) / sample_rate
    samples = amplitude * np.sin(2 * np.pi * 440.0 * time).astype(np.float32)
    soundfile.write(path, np.column_stack((samples, samples)), sample_rate, subtype="FLOAT")


def test_performance_templates_are_valid_and_round_trip() -> None:
    two_voice = routing_plan_from_template("two_voice")
    assert two_voice.max_voices == 2
    assert [route.id for route in two_voice.routes] == ["melody", "bass"]
    validate_routing_plan(two_voice.to_dict())
    loaded = load_routing_plan(two_voice.to_dict())
    assert loaded == two_voice

    full = routing_plan_from_template("full")
    assert not next(route for route in full.routes if route.id == "drums").enabled


def test_route_stem_audio_applies_gain_mute_and_solo(tmp_path: pathlib.Path) -> None:
    stems = {}
    for role, amplitude in {
        "vocals": 0.8,
        "drums": 0.7,
        "bass": 0.6,
        "other": 0.4,
    }.items():
        path = tmp_path / f"{role}.wav"
        _stem(path, amplitude)
        stems[role] = path
    plan = RoutingPlan(
        1,
        "custom",
        (
            StemRoute("muted-vocals", True, ("vocals",), "melody", muted=True),
            StemRoute("other", True, ("other",), "harmony", gain_db=-6.0, solo=True),
            StemRoute("ignored-drums", True, ("drums",), "ignore"),
        ),
        2,
    )
    output = route_stem_audio(stems, plan, tmp_path / "routed.wav")
    samples, sample_rate = soundfile.read(output, dtype="float32")
    assert sample_rate == 44_100
    assert len(samples) == 4_410
    expected_peak = 0.4 * (10 ** (-6.0 / 20.0))
    assert float(np.max(np.abs(samples))) == pytest.approx(expected_peak, rel=0.02)


def test_invalid_route_rejected() -> None:
    plan = RoutingPlan(1, "custom", (StemRoute("bad", True, ("piano",), "melody"),), 2)
    with pytest.raises(ValueError, match="stem sources"):
        plan.validate()


def test_merge_routed_sequences_enforces_priority_and_voice_cap() -> None:
    def sequence(pitch: int, velocity: int) -> NoteSequence:
        return NoteSequence(
            duration_us=1_000_000,
            notes=(Note(pitch, 100_000, 300_000, velocity, 0.9, 0, 0),),
            tempo_map=(),
            beat_grid=(),
            provenance=Provenance("audio", 0, "test", {}),
        )

    plan = RoutingPlan(
        1,
        "custom",
        (
            StemRoute("primary", True, ("other",), "melody", priority=90),
            StemRoute("secondary", True, ("bass",), "bass", priority=50),
            StemRoute("disabled", False, ("drums",), "percussion", priority=100),
        ),
        1,
    )
    merged = merge_routed_sequences(
        {"primary": sequence(72, 100), "secondary": sequence(48, 120)},
        plan,
    )
    assert [note.pitch for note in merged.notes] == [72]
    assert merged.provenance.parameters["routing"]["mode"] == "custom"
