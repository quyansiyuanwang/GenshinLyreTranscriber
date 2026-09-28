"""Validate the desktop-only event fixture without changing the worker protocol."""

import json
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_desktop_cancellation_fixture() -> None:
    schema = json.loads((ROOT / "schemas/desktop-cancellation-v1.schema.json").read_text())
    event = json.loads((ROOT / "tests/fixtures/desktop-cancellation-v1.json").read_text())
    jsonschema.validate(event, schema)
    for operation in ("analysis", "separation", "routing"):
        jsonschema.validate({**event, "operation": operation}, schema)
    for patch in ({"format_version": 2}, {"state": "cancelling"}, {"operation": "unknown"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**event, **patch}, schema)
