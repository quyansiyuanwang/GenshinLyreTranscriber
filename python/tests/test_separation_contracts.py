from __future__ import annotations

import hashlib
import json
import pathlib
import textwrap

import numpy as np
import pytest
import soundfile

from glt_core.protocol.validation import (
    validate_separator_component,
    validate_stem_set,
)
from glt_core.separation import (
    ComponentVerificationError,
    StemArtifact,
    StemSet,
    build_instrumental,
    load_component,
    verify_component,
)
from glt_core.separation.provider import run_component
from glt_core.separation.stem_set import validate_stem_set as load_stem_set


def _hash(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _component(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "component"
    root.mkdir()
    executable = root / "separator.exe"
    executable.write_bytes(b"runtime")
    license_file = root / "LICENSE.txt"
    license_file.write_text("MIT\n", encoding="utf-8")
    model = root / "models" / "htdemucs.onnx"
    model.parent.mkdir()
    model.write_bytes(b"model")
    manifest = {
        "format_version": 1,
        "component_id": "demucs-cpu",
        "component_version": "4.0.1",
        "protocol_version": 1,
        "runtime": {
            "executable": executable.name,
            "sha256": _hash(executable),
            "arguments": ["separate"],
        },
        "license": {"relative_path": license_file.name},
        "models": [
            {
                "id": "htdemucs",
                "quality": "balanced",
                "relative_path": "models/htdemucs.onnx",
                "sha256": _hash(model),
                "size_bytes": model.stat().st_size,
                "source_url": "https://example.invalid/htdemucs",
                "license_name": "MIT",
            }
        ],
    }
    (root / "separator-component-v1.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    validate_separator_component(manifest)
    return root


def test_component_verification_and_hash_failure(tmp_path: pathlib.Path) -> None:
    root = _component(tmp_path)
    manifest = load_component(root)
    assert manifest.component_id == "demucs-cpu"
    verified = verify_component(root, model_id="htdemucs")
    assert verified.command == (str((root / "separator.exe").resolve()), "separate")

    (root / "models" / "htdemucs.onnx").write_bytes(b"tampered")
    with pytest.raises(ComponentVerificationError, match="mismatch"):
        verify_component(root, model_id="htdemucs")


def _write_stem(path: pathlib.Path, frequency: float, sample_rate: int = 44_100) -> None:
    time = np.arange(sample_rate, dtype=np.float32) / sample_rate
    samples = (0.05 * np.sin(2 * np.pi * frequency * time)).astype(np.float32)
    soundfile.write(path, np.column_stack((samples, samples)), sample_rate, subtype="PCM_24")


def test_stem_set_and_instrumental_validation(tmp_path: pathlib.Path) -> None:
    roles = ("vocals", "drums", "bass", "other")
    frequencies = (440.0, 220.0, 110.0, 330.0)
    paths = {}
    for role, frequency in zip(roles, frequencies, strict=True):
        path = tmp_path / f"{role}.wav"
        _write_stem(path, frequency)
        paths[role] = path
    instrumental_path = build_instrumental(paths, tmp_path / "instrumental.wav")
    assert instrumental_path.is_file()

    def artifact(role: str, path: pathlib.Path) -> StemArtifact:
        info = soundfile.info(path)
        return StemArtifact(
            role=role,
            relative_path=path.name,
            sha256=_hash(path),
            size_bytes=path.stat().st_size,
            sample_rate=info.samplerate,
            channels=info.channels,
            duration_us=round(info.frames * 1_000_000 / info.samplerate),
        )

    stem_set = StemSet(
        format_version=1,
        source_sha256="a" * 64,
        component_id="demucs-cpu",
        component_version="4.0.1",
        model_id="htdemucs",
        model_sha256="b" * 64,
        quality="balanced",
        sample_rate=44_100,
        channels=2,
        stems=tuple(artifact(role, paths[role]) for role in roles),
        instrumental=artifact("instrumental", instrumental_path),
    )
    document = stem_set.to_dict()
    validate_stem_set(document)
    loaded = load_stem_set(document, tmp_path)
    assert loaded.sample_rate == 44_100
    assert [stem.role for stem in loaded.stems] == list(roles)

    paths["drums"].write_bytes(paths["drums"].read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_stem_set(document, tmp_path)


def test_component_provider_executes_and_validates_stem_set(
    tmp_path: pathlib.Path,
) -> None:
    root = _component(tmp_path)
    script = root / "separator.py"
    script.write_text(
        textwrap.dedent(
            """
            import argparse, hashlib, json, pathlib
            import numpy as np, soundfile

            parser = argparse.ArgumentParser()
            parser.add_argument("--input", required=True)
            parser.add_argument("--output", required=True)
            parser.add_argument("separate", nargs="?")
            parser.add_argument("--model", required=True)
            args = parser.parse_args()
            output = pathlib.Path(args.output)
            output.mkdir(parents=True, exist_ok=True)
            sr = 44100
            t = np.arange(sr // 10, dtype=np.float32) / sr
            freqs = {"vocals": 440.0, "drums": 220.0, "bass": 110.0, "other": 330.0}
            stems = []
            arrays = []
            for role, freq in freqs.items():
                samples = (0.05 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
                stereo = np.column_stack((samples, samples))
                path = output / f"{role}.wav"
                soundfile.write(path, stereo, sr, subtype="PCM_24")
                arrays.append(stereo)
                stems.append({
                    "role": role,
                    "relative_path": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size_bytes": path.stat().st_size,
                    "sample_rate": sr,
                    "channels": 2,
                    "duration_us": round(len(stereo) * 1_000_000 / sr),
                })
            instrumental = np.clip(np.sum(arrays[1:], axis=0), -1.0, 1.0)
            path = output / "instrumental.wav"
            soundfile.write(path, instrumental, sr, subtype="PCM_24")
            stem_set = {
                "format_version": 1,
                "source": {"sha256": "a" * 64},
                "separation": {
                    "component_id": "demucs-cpu",
                    "component_version": "4.0.1",
                    "model_id": args.model,
                    "model_sha256": "b" * 64,
                    "quality": "balanced",
                },
                "sample_rate": sr,
                "channels": 2,
                "stems": stems,
                "instrumental": {
                    "role": "instrumental",
                    "relative_path": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size_bytes": path.stat().st_size,
                    "sample_rate": sr,
                    "channels": 2,
                    "duration_us": round(len(instrumental) * 1_000_000 / sr),
                },
            }
            manifest = output / "stem-set-v1.json"
            manifest.write_text(json.dumps(stem_set), encoding="utf-8")
            print(
                json.dumps({"type": "progress", "stage": "separating", "fraction": 0.5}),
                flush=True,
            )
            print(
                json.dumps(
                    {
                        "type": "result",
                        "stem_set_path": str(manifest),
                        "elapsed_seconds": 0.1,
                    }
                ),
                flush=True,
            )
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    manifest_path = root / "separator-component-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"]["executable"] = script.name
    manifest["runtime"]["sha256"] = _hash(script)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    source = tmp_path / "input.wav"
    _write_stem(source, 440.0)

    run = run_component(root, source, tmp_path / "separated", model_id="htdemucs")
    assert run.stem_set.instrumental.role == "instrumental"
    assert [stem.role for stem in run.stem_set.stems] == ["vocals", "drums", "bass", "other"]
