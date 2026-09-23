"""Download and verify pinned binary resources used by portable builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import tempfile
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "resources/manifest.json"


def _load_resource(name: str) -> dict[str, object]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    try:
        resource = manifest["resources"][name]
    except KeyError as exc:
        raise SystemExit(f"Unknown resource: {name}") from exc
    if not isinstance(resource, dict):
        raise SystemExit(f"Invalid resource manifest entry: {name}")
    return resource


def _download(url: str, destination: pathlib.Path, expected_size: int, expected_hash: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
        temporary_path = pathlib.Path(temporary.name)
        try:
            with urllib.request.urlopen(url) as response:
                shutil.copyfileobj(response, temporary)
            temporary.close()
            if temporary_path.stat().st_size != expected_size:
                raise SystemExit(
                    f"Size mismatch for {destination.name}: {temporary_path.stat().st_size}"
                )
            digest = hashlib.sha256(temporary_path.read_bytes()).hexdigest()
            if digest != expected_hash:
                raise SystemExit(f"SHA256 mismatch for {destination.name}: {digest}")
            temporary_path.replace(destination)
        finally:
            temporary_path.unlink(missing_ok=True)


def fetch(name: str) -> pathlib.Path:
    resource = _load_resource(name)
    destination = ROOT / str(resource["destination"])
    if ROOT not in destination.resolve().parents:
        raise SystemExit(f"Destination escapes repository root: {destination}")
    if name == "ffmpeg_windows_x64":
        destination = destination / "ffmpeg-n8.1.3-win64-lgpl-shared-8.1.zip"
    _download(
        str(resource["url"]),
        destination,
        int(str(resource["size"])),
        str(resource["sha256"]),
    )
    if name == "ffmpeg_windows_x64":
        extract_root = destination.parent
        extract_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination) as archive:
            archive.extractall(extract_root)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("resource", choices=["basic-pitch", "ffmpeg"])
    args = parser.parse_args()
    name = "basic_pitch_onnx" if args.resource == "basic-pitch" else "ffmpeg_windows_x64"
    print(fetch(name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
