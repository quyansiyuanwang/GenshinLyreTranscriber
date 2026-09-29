"""Checked schema inventory for frozen workers; no hard-coded schema count."""

import hashlib
import json
from pathlib import Path


def schema_bundle_data(schema_dir: Path) -> list[tuple[str, str]]:
    manifest_path = schema_dir / "versions.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("status") != "frozen":
        raise ValueError("Schema manifest must be frozen")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Schema manifest has no files")
    for name, digest in files.items():
        if (
            not isinstance(name, str)
            or "/" in name
            or "\\" in name
            or not name.endswith(".schema.json")
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ValueError("Unsafe schema path or invalid checksum in manifest")
    actual = {path.name for path in schema_dir.glob("*.schema.json")}
    expected = set(files)
    if actual != expected:
        raise ValueError(
            f"Schema inventory mismatch: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )
    result = []
    for name in sorted(files):
        path = schema_dir / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != files[name]:
            raise ValueError(f"Schema checksum mismatch or symbolic link: {name}")
        result.append((str(path), "glt_core/schemas"))
    result.append((str(manifest_path), "glt_core/schemas"))
    return result
