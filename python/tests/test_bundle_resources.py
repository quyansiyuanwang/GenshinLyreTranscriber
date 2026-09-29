import hashlib
import json
from pathlib import Path

import pytest

from glt_core.bundle_resources import schema_bundle_data


def inventory(root: Path, count: int) -> dict[str, object]:
    files = {}
    for index in range(count):
        name = f"contract-{index}.schema.json"
        content = b'{"type":"object"}\n'
        (root / name).write_bytes(content)
        files[name] = hashlib.sha256(content).hexdigest()
    manifest: dict[str, object] = {"schema_version": 3, "status": "frozen", "files": files}
    (root / "versions.json").write_text(json.dumps(manifest))
    return manifest


@pytest.mark.parametrize("count", [1, 15, 17, 20])
def test_bundle_uses_frozen_inventory_not_fixed_count(tmp_path: Path, count: int) -> None:
    inventory(tmp_path, count)
    data = schema_bundle_data(tmp_path)
    assert len(data) == count + 1
    assert all(destination == "glt_core/schemas" for _, destination in data)
    assert Path(data[-1][0]).name == "versions.json"


@pytest.mark.parametrize("fault", ["missing", "extra", "changed", "escape", "unfrozen"])
def test_bundle_rejects_inconsistent_manifest(tmp_path: Path, fault: str) -> None:
    manifest = inventory(tmp_path, 1)
    if fault == "missing":
        (tmp_path / "contract-0.schema.json").unlink()
    elif fault == "extra":
        (tmp_path / "extra.schema.json").write_text("{}")
    elif fault == "changed":
        (tmp_path / "contract-0.schema.json").write_text("{}")
    elif fault == "escape":
        manifest["files"] = {"../escape.schema.json": "a" * 64}
        (tmp_path / "versions.json").write_text(json.dumps(manifest))
    else:
        manifest["status"] = "draft"
        (tmp_path / "versions.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        schema_bundle_data(tmp_path)
