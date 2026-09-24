"""Offline verification for optional separator runtime packages."""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass
from typing import Any, Literal

Quality = Literal["fast", "balanced", "high_quality"]
MODEL_QUALITIES = {"fast", "balanced", "high_quality"}
STEM_ROLES = ("vocals", "drums", "bass", "other")

COMPONENT_MANIFEST_NAME = "separator-component-v1.json"


class ComponentVerificationError(ValueError):
    """Raised when a separator component cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ModelPackage:
    id: str
    quality: Quality
    relative_path: pathlib.Path
    sha256: str
    logical_sha256: str
    size_bytes: int
    source_url: str
    license_name: str


@dataclass(frozen=True, slots=True)
class ComponentManifest:
    format_version: int
    component_id: str
    component_version: str
    protocol_version: int
    executable: pathlib.Path
    executable_sha256: str
    arguments: tuple[str, ...]
    license_file: pathlib.Path
    models: tuple[ModelPackage, ...]

    def model(self, model_id: str) -> ModelPackage:
        selected = next((item for item in self.models if item.id == model_id), None)
        if selected is None:
            raise ComponentVerificationError(f"separator model is not installed: {model_id}")
        return selected


@dataclass(frozen=True, slots=True)
class VerifiedComponent:
    directory: pathlib.Path
    manifest: ComponentManifest
    executable: pathlib.Path
    model: ModelPackage | None

    @property
    def command(self) -> tuple[str, ...]:
        return (str(self.executable), *self.manifest.arguments)


def load_component(directory: pathlib.Path | str) -> ComponentManifest:
    root = pathlib.Path(directory).expanduser().resolve()
    manifest_path = root / COMPONENT_MANIFEST_NAME
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ComponentVerificationError(f"separator component is not installed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ComponentVerificationError(f"invalid separator manifest: {exc}") from exc
    if not isinstance(document, dict) or document.get("format_version") != 1:
        raise ComponentVerificationError("unsupported separator component manifest")
    runtime = _mapping(document, "runtime")
    license_document = _mapping(document, "license")
    models_document = document.get("models")
    if not isinstance(models_document, list) or not models_document:
        raise ComponentVerificationError("separator manifest must contain models")
    models: list[ModelPackage] = []
    for item in models_document:
        if not isinstance(item, dict):
            raise ComponentVerificationError("separator model entry must be an object")
        quality = str(item.get("quality", ""))
        if quality not in MODEL_QUALITIES:
            raise ComponentVerificationError(f"invalid separator model quality: {quality}")
        model_id = _required_string(item, "id")
        if any(existing.id == model_id for existing in models):
            raise ComponentVerificationError(f"duplicate separator model: {model_id}")
        models.append(
            ModelPackage(
                id=model_id,
                quality=quality,  # type: ignore[arg-type]
                relative_path=_safe_relative(item, "relative_path"),
                sha256=_sha256(item, "sha256"),
                logical_sha256=_sha256(item, "logical_sha256"),
                size_bytes=_non_negative_int(item, "size_bytes"),
                source_url=_required_string(item, "source_url"),
                license_name=_required_string(item, "license_name"),
            )
        )
    arguments = runtime.get("arguments", [])
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        raise ComponentVerificationError("separator runtime arguments must be strings")
    return ComponentManifest(
        format_version=1,
        component_id=_required_string(document, "component_id"),
        component_version=_required_string(document, "component_version"),
        protocol_version=_non_negative_int(document, "protocol_version"),
        executable=_safe_relative(runtime, "executable"),
        executable_sha256=_sha256(runtime, "sha256"),
        arguments=tuple(arguments),
        license_file=_safe_relative(license_document, "relative_path"),
        models=tuple(models),
    )


def verify_component(
    directory: pathlib.Path | str,
    *,
    model_id: str | None = None,
    verify_hashes: bool = True,
) -> VerifiedComponent:
    root = pathlib.Path(directory).expanduser().resolve()
    manifest = load_component(root)
    executable = _verified_file(
        root,
        manifest.executable,
        expected_size=None,
        expected_hash=manifest.executable_sha256 if verify_hashes else None,
    )
    _verified_file(root, manifest.license_file, expected_size=None, expected_hash=None)
    selected = manifest.model(model_id) if model_id is not None else None
    if selected is not None:
        _verified_file(
            root,
            selected.relative_path,
            expected_size=selected.size_bytes,
            expected_hash=selected.sha256 if verify_hashes else None,
        )
    return VerifiedComponent(
        directory=root,
        manifest=manifest,
        executable=executable,
        model=selected,
    )


def sha256_file(path: pathlib.Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _verified_file(
    root: pathlib.Path,
    relative: pathlib.Path,
    *,
    expected_size: int | None,
    expected_hash: str | None,
) -> pathlib.Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ComponentVerificationError(f"separator path escapes component: {relative}")
    if not path.is_file():
        raise ComponentVerificationError(f"separator component file is missing: {relative}")
    if expected_size is not None and path.stat().st_size != expected_size:
        raise ComponentVerificationError(f"separator file size mismatch: {relative}")
    if expected_hash is not None and sha256_file(path) != expected_hash:
        raise ComponentVerificationError(f"separator file hash mismatch: {relative}")
    return path


def _mapping(document: dict[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise ComponentVerificationError(f"separator manifest field {key} must be an object")
    return value


def _required_string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ComponentVerificationError(f"separator manifest field {key} is required")
    return value


def _safe_relative(document: dict[str, Any], key: str) -> pathlib.Path:
    value = pathlib.Path(_required_string(document, key))
    if value.is_absolute() or any(part == ".." for part in value.parts):
        raise ComponentVerificationError(f"separator path must be relative: {value}")
    return value


def _sha256(document: dict[str, Any], key: str) -> str:
    value = _required_string(document, key).lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ComponentVerificationError(f"separator manifest field {key} must be SHA256")
    return value


def _non_negative_int(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ComponentVerificationError(f"separator manifest field {key} is invalid")
    return value
