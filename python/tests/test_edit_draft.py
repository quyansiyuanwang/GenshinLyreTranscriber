"""Shared desktop recovery fixture; no worker protocol changes."""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[2]


def test_edit_draft_contract() -> None:
    schema = json.loads((ROOT / "schemas/edit-draft-v1.schema.json").read_text())
    performance = json.loads((ROOT / "schemas/performance-v1.schema.json").read_text())
    draft = json.loads((ROOT / "tests/fixtures/edit-draft-v1.json").read_text())
    registry = Registry().with_resource(performance["$id"], Resource.from_contents(performance))
    validator = jsonschema.Draft202012Validator(schema, registry=registry)
    validator.validate(draft)
    for patch in ({"format_version": 2}, {"base_sha256": "bad"}, {"unexpected": True}):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate({**draft, **patch})
    invalid = copy.deepcopy(draft)
    invalid["performance"]["notes"][0]["velocity"] = 200
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
