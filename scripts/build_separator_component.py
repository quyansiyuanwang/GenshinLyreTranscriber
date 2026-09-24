"""Build an offline separator component from the PyInstaller worker and pinned models."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import pathlib
import shutil
import urllib.request

MODELS = {
    "mdx_q": ("adefossez/Demucs-mdx_q", "fast"),
    "htdemucs": ("adefossez/HTDemucs", "balanced"),
    "htdemucs_ft": ("adefossez/HTDemucs-ft", "high_quality"),
}
DEMUCS_LICENSE_URL = (
    "https://raw.githubusercontent.com/facebookresearch/demucs/"
    "ef66d254cd6d558e207eeff2c4b8d053db2e77dd/LICENSE"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--models", nargs="+", choices=sorted(MODELS), required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    worker_dir = pathlib.Path(args.worker_dir).expanduser().resolve()
    output = pathlib.Path(args.output).expanduser().resolve()
    executable_name = "glt-separator-worker.exe" if os.name == "nt" else "glt-separator-worker"
    executable = worker_dir / executable_name
    if not executable.is_file():
        raise SystemExit(f"separator worker executable is missing: {executable}")
    if output.exists() and not args.overwrite:
        raise SystemExit(f"output already exists: {output}")
    staging = output.with_name(f".{output.name}.partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        worker_target = staging / "worker"
        shutil.copytree(worker_dir, worker_target)
        licenses_dir = staging / "licenses"
        licenses_dir.mkdir()
        _write_demucs_license(licenses_dir / "DEMUCS-LICENSE.txt")
        _copy_pytorch_license(licenses_dir / "PYTORCH-LICENSE.txt")
        (licenses_dir / "THIRD_PARTY.txt").write_text(
            "Demucs 4.1.0: MIT\nPyTorch 2.11.0: BSD-3-Clause\n"
            "See DEMUCS-LICENSE.txt and PYTORCH-LICENSE.txt.\n",
            encoding="utf-8",
        )
        model_entries = []
        model_cache = worker_target / "models"
        previous_hf_home = os.environ.get("HF_HOME")
        os.environ["HF_HOME"] = str(model_cache)
        try:
            for model_id in args.models:
                model_entries.append(_fetch_model(model_id, model_cache))
        finally:
            if previous_hf_home is None:
                os.environ.pop("HF_HOME", None)
            else:
                os.environ["HF_HOME"] = previous_hf_home
        manifest = {
            "format_version": 1,
            "component_id": "demucs-cpu",
            "component_version": "4.1.0",
            "protocol_version": 1,
            "runtime": {
                "executable": f"worker/{executable_name}",
                "sha256": _sha256(worker_target / executable_name),
                "arguments": ["--offline", "--model-dir", "worker/models"],
            },
            "license": {"relative_path": "licenses/THIRD_PARTY.txt"},
            "models": model_entries,
        }
        (staging / "separator-component-v1.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if output.exists():
            shutil.rmtree(output)
        shutil.move(str(staging), str(output))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(output)
    return 0


def _fetch_model(model_id: str, cache_root: pathlib.Path) -> dict[str, object]:
    from huggingface_hub import HfApi, hf_hub_download

    repo_id, quality = MODELS[model_id]
    revision = HfApi().model_info(repo_id).sha
    yaml_path = pathlib.Path(
        hf_hub_download(repo_id, f"{model_id}.yaml", revision=revision)
    )
    bag = json.loads(json.dumps(_yaml_load(yaml_path)))
    signatures = bag.get("models")
    if not isinstance(signatures, list) or not signatures:
        raise SystemExit(f"invalid Demucs bag: {repo_id}")
    files = [
        pathlib.Path(
            hf_hub_download(repo_id, f"{signature}.safetensors", revision=revision)
        )
        for signature in signatures
    ]
    file_entries = [
        {
            "relative_path": path.relative_to(cache_root).as_posix(),
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
        }
        for path in files
    ]
    logical_sha256 = hashlib.sha256(
        "\n".join(
            f"{path.name}:{_sha256(path)}"
            for path in sorted(files, key=lambda value: value.name)
        ).encode()
    ).hexdigest()
    bundle = {
        "format_version": 1,
        "model_id": model_id,
        "quality": quality,
        "repo_id": repo_id,
        "repo_revision": revision,
        "logical_sha256": logical_sha256,
        "files": file_entries,
    }
    bundle_path = cache_root / f"{model_id}.bundle.json"
    bundle_path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    return {
        "id": model_id,
        "quality": quality,
        "relative_path": bundle_path.relative_to(cache_root.parents[1]).as_posix(),
        "sha256": _sha256(bundle_path),
        "logical_sha256": logical_sha256,
        "size_bytes": bundle_path.stat().st_size,
        "source_url": f"https://huggingface.co/{repo_id}/tree/{revision}",
        "license_name": "MIT",
    }


def _yaml_load(path: pathlib.Path) -> object:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_demucs_license(path: pathlib.Path) -> None:
    with urllib.request.urlopen(DEMUCS_LICENSE_URL, timeout=30) as response:
        path.write_bytes(response.read())


def _copy_pytorch_license(path: pathlib.Path) -> None:
    distribution = importlib.metadata.distribution("torch")
    candidates = list(distribution.locate_file("").rglob("*LICENSE*"))
    for candidate in candidates:
        if candidate.is_file() and "torch" in str(candidate).lower():
            shutil.copyfile(candidate, path)
            return
    raise SystemExit("cannot locate PyTorch license text")


def _sha256(path: pathlib.Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
