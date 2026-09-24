"""Stem set contract and instrumental reconstruction."""

from __future__ import annotations

import json
import os
import pathlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import soundfile

from glt_core.separation.component import STEM_ROLES, sha256_file


@dataclass(frozen=True, slots=True)
class StemArtifact:
    role: str
    relative_path: str
    sha256: str
    size_bytes: int
    sample_rate: int
    channels: int
    duration_us: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StemSet:
    format_version: int
    source_sha256: str
    component_id: str
    component_version: str
    model_id: str
    model_sha256: str
    quality: str
    sample_rate: int
    channels: int
    stems: tuple[StemArtifact, ...]
    instrumental: StemArtifact

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "source": {"sha256": self.source_sha256},
            "separation": {
                "component_id": self.component_id,
                "component_version": self.component_version,
                "model_id": self.model_id,
                "model_sha256": self.model_sha256,
                "quality": self.quality,
            },
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "stems": [stem.to_dict() for stem in self.stems],
            "instrumental": self.instrumental.to_dict(),
        }


def build_instrumental(
    stems: Mapping[str, pathlib.Path | str],
    destination: pathlib.Path | str,
) -> pathlib.Path:
    missing = [role for role in ("drums", "bass", "other") if role not in stems]
    if missing:
        raise ValueError(f"instrumental requires stems: {', '.join(missing)}")
    loaded: list[np.ndarray] = []
    sample_rate: int | None = None
    for role in ("drums", "bass", "other"):
        samples, current_rate = soundfile.read(stems[role], dtype="float32", always_2d=True)
        if sample_rate is None:
            sample_rate = current_rate
        elif current_rate != sample_rate:
            raise ValueError("stem sample rates do not match")
        loaded.append(samples)
    if not loaded or sample_rate is None:
        raise ValueError("no stems were loaded")
    shape = loaded[0].shape
    if any(samples.shape != shape for samples in loaded[1:]):
        raise ValueError("stem lengths/channels do not match")
    mixed = np.sum(loaded, axis=0)
    mixed = np.clip(mixed, -1.0, 1.0)
    output = pathlib.Path(destination).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial{output.suffix}")
    partial.unlink(missing_ok=True)
    try:
        soundfile.write(partial, mixed, sample_rate, subtype="PCM_24")
        os.replace(partial, output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return output


def validate_stem_set(document: Any, directory: pathlib.Path | str) -> StemSet:
    root = pathlib.Path(directory).expanduser().resolve()
    if not isinstance(document, dict) or document.get("format_version") != 1:
        raise ValueError("unsupported stem set format")
    stems_document = document.get("stems")
    if not isinstance(stems_document, list):
        raise ValueError("stem set stems must be an array")
    roles = [str(item.get("role")) for item in stems_document if isinstance(item, dict)]
    if tuple(roles) != STEM_ROLES:
        raise ValueError("stem set must contain vocals, drums, bass and other in order")
    stems = tuple(_artifact(item, root) for item in stems_document)
    instrumental_document = document.get("instrumental")
    if not isinstance(instrumental_document, dict):
        raise ValueError("stem set instrumental is required")
    instrumental = _artifact(instrumental_document, root)
    reference = stems[0]
    for stem in (*stems[1:], instrumental):
        if stem.sample_rate != reference.sample_rate or stem.channels != reference.channels:
            raise ValueError("stem formats do not match")
        if abs(stem.duration_us - reference.duration_us) > 1_000:
            raise ValueError("stem durations do not match")
    separation = document.get("separation")
    source = document.get("source")
    if not isinstance(separation, dict) or not isinstance(source, dict):
        raise ValueError("stem set metadata is invalid")
    return StemSet(
        format_version=1,
        source_sha256=str(source.get("sha256", "")),
        component_id=str(separation.get("component_id", "")),
        component_version=str(separation.get("component_version", "")),
        model_id=str(separation.get("model_id", "")),
        model_sha256=str(separation.get("model_sha256", "")),
        quality=str(separation.get("quality", "")),
        sample_rate=reference.sample_rate,
        channels=reference.channels,
        stems=stems,
        instrumental=instrumental,
    )


def write_stem_set(document: dict[str, Any], destination: pathlib.Path | str) -> pathlib.Path:
    output = pathlib.Path(destination).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        partial.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(partial, output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return output


def _artifact(document: Any, root: pathlib.Path) -> StemArtifact:
    if not isinstance(document, dict):
        raise ValueError("stem artifact must be an object")
    relative = pathlib.Path(str(document.get("relative_path", "")))
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise ValueError("stem path must be relative")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"stem file is missing: {relative}")
    expected_hash = str(document.get("sha256", ""))
    if expected_hash != sha256_file(path):
        raise ValueError(f"stem hash mismatch: {relative}")
    info = soundfile.info(path)
    return StemArtifact(
        role=str(document.get("role", "instrumental")),
        relative_path=relative.as_posix(),
        sha256=expected_hash,
        size_bytes=path.stat().st_size,
        sample_rate=info.samplerate,
        channels=info.channels,
        duration_us=round(info.frames * 1_000_000 / info.samplerate),
    )
