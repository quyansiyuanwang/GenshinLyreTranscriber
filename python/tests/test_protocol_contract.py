from __future__ import annotations

import hashlib
import json
import pathlib
from collections.abc import Callable
from typing import Any, cast

import pytest

from glt_core.domain.note_sequence import Note, NoteSequence, Provenance
from glt_core.protocol.validation import (
    ProtocolValidationError,
    parse_json_text,
    validate_events,
    validate_note_sequence,
    validate_report,
    validate_schema,
    validate_worker_message,
)

PROTOCOL_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "tests/fixtures/protocol/v1"
VALIDATORS: dict[str, Callable[[Any], None]] = {
    "events-v1.schema.json": validate_events,
    "worker-v1.schema.json": validate_worker_message,
    "note-sequence-v1.schema.json": validate_note_sequence,
    "report-v1.schema.json": validate_report,
}


def _load_cases() -> dict[str, Any]:
    document = parse_json_text((PROTOCOL_FIXTURES / "cases.json").read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError("protocol case manifest must be an object")
    return cast("dict[str, Any]", document)


def _load_document(case: dict[str, Any]) -> Any:
    if "document" in case:
        return case["document"]
    fixture = PROTOCOL_FIXTURES / str(case["fixture"])
    return parse_json_text(fixture.read_text(encoding="utf-8-sig"))


@pytest.mark.parametrize("case", _load_cases()["cases"], ids=lambda case: str(case["name"]))
def test_protocol_contract_cases(case: dict[str, Any]) -> None:
    document = _load_document(case)
    schema_file = str(case["schema_file"])
    schema_valid = True
    try:
        validate_schema(document, schema_file)
    except ProtocolValidationError as exc:
        schema_valid = False
        assert exc.code == "SCHEMA_INVALID"
    assert schema_valid is case["schema_valid"]

    if case["semantic_valid"] is None:
        return

    validator = VALIDATORS[schema_file]
    if case["semantic_valid"]:
        validator(document)
    else:
        with pytest.raises(ProtocolValidationError) as captured:
            validator(document)
        assert captured.value.code == case["expected_error"]


@pytest.mark.parametrize(
    "case", _load_cases()["raw_json_cases"], ids=lambda case: str(case["name"])
)
def test_raw_json_rejection(case: dict[str, Any]) -> None:
    text = (PROTOCOL_FIXTURES / str(case["fixture"])).read_text(encoding="utf-8-sig")
    with pytest.raises(ProtocolValidationError) as captured:
        parse_json_text(text)
    assert captured.value.code == case["expected_error"]


def test_event_dispatch_expectations() -> None:
    for case in _load_cases()["cases"]:
        if case["schema_file"] != "events-v1.schema.json" or not case["semantic_valid"]:
            continue
        document = _load_document(case)
        assert [event["at_us"] for event in document["events"]] == case["expected_dispatch_us"]
        assert document["duration_us"] == case["expected_finish_us"]


def test_note_sequence_dataclass_matches_schema() -> None:
    sequence = NoteSequence(
        duration_us=1_000_000,
        notes=(Note(69, 0, 500_000, 100, 0.9, 0, 0),),
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance("audio", 0, "basic-pitch-0.4.0", {}),
    )
    sequence.validate()
    assert json.loads(json.dumps(sequence.to_dict()))["schema_version"] == 1


def test_note_sequence_dataclass_rejects_bad_range() -> None:
    sequence = NoteSequence(
        duration_us=100_000,
        notes=(Note(69, 50_000, 200_000, 100, None, 0, 0),),
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance("midi", 0, None, {}),
    )
    with pytest.raises(ProtocolValidationError, match="duration"):
        sequence.validate()


def test_schema_hash_manifest_matches_files() -> None:
    schemas = PROTOCOL_FIXTURES.parents[3] / "schemas"
    manifest = cast(
        "dict[str, Any]",
        parse_json_text((schemas / "versions.json").read_text(encoding="utf-8")),
    )
    assert manifest["status"] == "frozen"
    for filename, expected_hash in manifest["files"].items():
        digest = hashlib.sha256((schemas / str(filename)).read_bytes()).hexdigest()
        assert digest == expected_hash
