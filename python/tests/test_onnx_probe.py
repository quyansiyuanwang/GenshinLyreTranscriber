from __future__ import annotations

import hashlib
import pathlib

import pytest

from glt_core.transcription import onnx_probe
from glt_core.transcription.onnx_probe import ModelResourceError, resolve_model_path


def test_resolve_model_path_rejects_missing_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(onnx_probe.MODEL_ENV_VAR, raising=False)
    monkeypatch.setattr(onnx_probe, "_installed_model_path", lambda: tmp_path / "missing.onnx")
    with pytest.raises(ModelResourceError, match="not found"):
        resolve_model_path(tmp_path / "missing.onnx")


def test_resolve_model_path_rejects_wrong_hash(tmp_path: pathlib.Path) -> None:
    model = tmp_path / "nmp.onnx"
    content = b"x" * 230_444
    model.write_bytes(content)
    assert hashlib.sha256(content).hexdigest() != (
        "2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec"
    )
    with pytest.raises(ModelResourceError, match="SHA256 mismatch"):
        resolve_model_path(model)
