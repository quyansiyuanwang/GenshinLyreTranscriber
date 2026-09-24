"""Packaged CPU Demucs adapter for the optional separator component."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from glt_core.separation.component import sha256_file
from glt_core.separation.stem_set import (
    StemArtifact,
    StemSet,
    build_instrumental,
    validate_stem_set,
    write_stem_set,
)

SUPPORTED_MODELS = {"mdx_q", "htdemucs", "htdemucs_ft"}
QUALITY_BY_MODEL = {"mdx_q": "fast", "htdemucs": "balanced", "htdemucs_ft": "high_quality"}
STEM_ROLES = ("vocals", "drums", "bass", "other")


class DemucsError(RuntimeError):
    """Demucs runtime failure."""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glt-separator-worker")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", choices=sorted(SUPPORTED_MODELS), default="htdemucs")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--model-dir")
    args = parser.parse_args(argv)
    try:
        model_directory = None
        if args.model_dir:
            model_directory = pathlib.Path(args.model_dir).expanduser().resolve()
            model_directory.mkdir(parents=True, exist_ok=True)
            os.environ["HF_HOME"] = str(model_directory)
            model_directory = model_directory / "hub"
        run = separate_demucs(
            pathlib.Path(args.input),
            pathlib.Path(args.output),
            model_id=args.model,
            device=args.device,
            offline=args.offline,
            model_cache_dir=model_directory,
            progress=lambda stage, fraction: _write(
                {"type": "progress", "stage": stage, "fraction": fraction}
            ),
        )
    except (DemucsError, OSError, ValueError, RuntimeError) as exc:
        _write({"type": "error", "message": str(exc)})
        return 2
    _write(
        {
            "type": "result",
            "stem_set_path": str(run[0]),
            "elapsed_seconds": run[1],
            "model_sha256": run[2],
        }
    )
    return 0


def separate_demucs(
    input_path: pathlib.Path | str,
    output_directory: pathlib.Path | str,
    *,
    model_id: str,
    device: str = "cpu",
    offline: bool = False,
    model_cache_dir: pathlib.Path | None = None,
    progress: Callable[[str, float | None], None] | None = None,
) -> tuple[pathlib.Path, float, str]:
    if model_id not in SUPPORTED_MODELS:
        raise DemucsError(f"unsupported Demucs model: {model_id}")
    source = pathlib.Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise DemucsError(f"input audio does not exist: {source}")
    output = pathlib.Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if progress is not None:
        progress("validating-model", 0.0)
    revision = _bundled_revision(model_cache_dir, model_id)
    model_hash, model_paths = model_bundle_sha256(
        model_id,
        offline=offline,
        revision=revision,
        cache_dir=model_cache_dir,
    )
    if progress is not None:
        progress("separating", 0.05)

    try:
        import torch
        from demucs.separate import main as demucs_main
    except ImportError as exc:
        raise DemucsError("Demucs separator runtime is not installed") from exc
    torch.set_num_threads(max(1, min(8, os.cpu_count() or 1)))
    temporary_root = pathlib.Path(tempfile.mkdtemp(prefix=".glt-demucs-", dir=output))
    try:
        arguments = [
            "--out",
            str(temporary_root),
            "--name",
            model_id,
            "--device",
            device,
            "--shifts",
            "0",
            str(source),
        ]
        with (
            _demucs_environment(model_paths, offline=offline),
            contextlib.redirect_stdout(sys.stderr),
        ):
            try:
                demucs_main(arguments)
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    raise DemucsError(f"Demucs exited with code {exc.code}") from exc
        separated = temporary_root / model_id
        track_directories = [path for path in separated.iterdir() if path.is_dir()]
        if len(track_directories) != 1:
            raise DemucsError("Demucs did not produce exactly one track directory")
        track_directory = track_directories[0]
        standardized: dict[str, pathlib.Path] = {}
        for role in STEM_ROLES:
            matches = list(track_directory.rglob(f"{role}.*"))
            if not matches:
                raise DemucsError(f"Demucs did not produce the {role} stem")
            destination = output / f"{role}.wav"
            shutil.copyfile(matches[0], destination)
            standardized[role] = destination
        if progress is not None:
            progress("reconstructing-instrumental", 0.9)
        instrumental_path = build_instrumental(standardized, output / "instrumental.wav")
        artifacts = tuple(_artifact(role, standardized[role], output) for role in STEM_ROLES)
        instrumental = _artifact("instrumental", instrumental_path, output)
        stem_set = StemSet(
            format_version=1,
            source_sha256=sha256_file(source),
            component_id="demucs-cpu",
            component_version="4.1.0",
            model_id=model_id,
            model_sha256=model_hash,
            quality=QUALITY_BY_MODEL[model_id],
            sample_rate=44_100,
            channels=2,
            stems=artifacts,
            instrumental=instrumental,
        )
        document = stem_set.to_dict()
        stem_set_path = write_stem_set(document, output / "stem-set-v1.json")
        validate_stem_set(document, output)
        elapsed = time.perf_counter() - started
        if progress is not None:
            progress("completed", 1.0)
        return stem_set_path, elapsed, model_hash
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def model_bundle_sha256(
    model_id: str,
    *,
    offline: bool,
    revision: str | None = None,
    cache_dir: pathlib.Path | None = None,
) -> tuple[str, tuple[pathlib.Path, ...]]:
    import yaml  # type: ignore[import-untyped]

    try:
        from demucs.hf import hf_repo_name
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise DemucsError("Hugging Face model runtime is not installed") from exc
    repo_id = f"adefossez/{hf_repo_name(model_id)}"
    yaml_path = pathlib.Path(
        hf_hub_download(
            repo_id,
            f"{model_id}.yaml",
            revision=revision,
            local_files_only=offline,
            cache_dir=cache_dir,
        )
    )
    bag = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    signatures = bag.get("models") if isinstance(bag, dict) else None
    if not isinstance(signatures, list) or not signatures:
        raise DemucsError("Demucs bag definition has no model signatures")
    files = tuple(
        pathlib.Path(
            hf_hub_download(
                repo_id,
                f"{signature}.safetensors",
                revision=revision,
                local_files_only=offline,
                cache_dir=cache_dir,
            )
        )
        for signature in signatures
    )
    digest_input = "\n".join(
        f"{path.name}:{sha256_file(path)}" for path in sorted(files, key=lambda value: value.name)
    )
    import hashlib

    return hashlib.sha256(digest_input.encode()).hexdigest(), files


def _bundled_revision(
    cache_dir: pathlib.Path | None,
    model_id: str,
) -> str | None:
    if cache_dir is None:
        return None
    bundle = cache_dir.parent / f"{model_id}.bundle.json"
    try:
        document = json.loads(bundle.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    revision = document.get("repo_revision") if isinstance(document, dict) else None
    return revision if isinstance(revision, str) else None


@contextlib.contextmanager
def _demucs_environment(
    model_paths: tuple[pathlib.Path, ...],
    *,
    offline: bool,
) -> Iterator[tuple[pathlib.Path, ...]]:
    previous = {key: os.environ.get(key) for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        yield model_paths
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _artifact(role: str, path: pathlib.Path, root: pathlib.Path) -> StemArtifact:
    import soundfile

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


def _write(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
