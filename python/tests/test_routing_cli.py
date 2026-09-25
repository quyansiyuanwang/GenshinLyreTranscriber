from __future__ import annotations

import json
import pathlib

import numpy as np
import pytest
import soundfile

from glt_core.routing_cli import main
from glt_core.separation.component import sha256_file
from glt_core.separation.stem_set import StemArtifact, StemSet, write_stem_set


def _artifact(role: str, path: pathlib.Path, root: pathlib.Path) -> StemArtifact:
    info = soundfile.info(path)
    return StemArtifact(
        role=role,
        relative_path=path.relative_to(root).as_posix(),
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        sample_rate=info.samplerate,
        channels=info.channels,
        duration_us=round(info.frames * 1_000_000 / info.samplerate),
    )


def test_route_cli_writes_merged_audio_and_plan(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sample_rate = 44_100
    frames = sample_rate // 2
    for role, frequency in (
        ("vocals", 440.0),
        ("drums", 80.0),
        ("bass", 110.0),
        ("other", 220.0),
    ):
        time = np.arange(frames, dtype=np.float32) / sample_rate
        tone = np.sin(2.0 * np.pi * frequency * time).astype(np.float32)
        soundfile.write(tmp_path / f"{role}.wav", np.stack((tone, tone), axis=1), sample_rate)
    stems = tuple(
        _artifact(role, tmp_path / f"{role}.wav", tmp_path)
        for role in ("vocals", "drums", "bass", "other")
    )
    instrumental = _artifact("instrumental", tmp_path / "other.wav", tmp_path)
    stem_set = StemSet(
        format_version=1,
        source_sha256="a" * 64,
        component_id="test",
        component_version="1",
        model_id="test",
        model_sha256="b" * 64,
        quality="balanced",
        sample_rate=sample_rate,
        channels=2,
        stems=stems,
        instrumental=instrumental,
    )
    stem_set_path = write_stem_set(stem_set.to_dict(), tmp_path / "stem-set-v1.json")
    output = tmp_path / "routing"
    assert (
        main(
            [
                "--stem-set",
                str(stem_set_path),
                "--output",
                str(output),
                "--mode",
                "two_voice",
            ]
        )
        == 0
    )
    assert '"type":"result"' in capsys.readouterr().out
    assert (output / "routed-mix.wav").is_file()
    plan = (output / "routing-plan-v1.json").read_text(encoding="utf-8")
    assert '"mode": "two_voice"' in plan


def test_route_cli_accepts_custom_plan_json(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sample_rate = 44_100
    frames = sample_rate // 10
    for role, frequency in (("vocals", 440.0), ("drums", 80.0), ("bass", 110.0), ("other", 220.0)):
        time = np.arange(frames, dtype=np.float32) / sample_rate
        tone = np.sin(2.0 * np.pi * frequency * time).astype(np.float32)
        soundfile.write(tmp_path / f"{role}.wav", np.stack((tone, tone), axis=1), sample_rate)
    stems = tuple(
        _artifact(role, tmp_path / f"{role}.wav", tmp_path)
        for role in ("vocals", "drums", "bass", "other")
    )
    stem_set = StemSet(
        format_version=1,
        source_sha256="a" * 64,
        component_id="test",
        component_version="1",
        model_id="test",
        model_sha256="b" * 64,
        quality="balanced",
        sample_rate=sample_rate,
        channels=2,
        stems=stems,
        instrumental=_artifact("instrumental", tmp_path / "other.wav", tmp_path),
    )
    stem_set_path = write_stem_set(stem_set.to_dict(), tmp_path / "stem-set-v1.json")
    plan = {
        "format_version": 1,
        "mode": "custom",
        "max_voices": 1,
        "collision": "priority",
        "routes": [
            {
                "id": "vocals-only",
                "enabled": True,
                "sources": ["vocals"],
                "target": "melody",
                "gain_db": -3.0,
                "muted": False,
                "solo": True,
                "priority": 90,
                "start_us": None,
                "end_us": None,
            }
        ],
    }
    output = tmp_path / "custom-routing"
    assert (
        main(
            [
                "--stem-set",
                str(stem_set_path),
                "--output",
                str(output),
                "--plan-json",
                json.dumps(plan),
            ]
        )
        == 0
    )
    assert '"type":"result"' in capsys.readouterr().out
    written = json.loads((output / "routing-plan-v1.json").read_text(encoding="utf-8"))
    assert written["mode"] == "custom"
    assert written["routes"][0]["gain_db"] == -3.0
