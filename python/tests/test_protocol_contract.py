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


def _validate_worker_v1(document: Any) -> None:
    if not isinstance(document, dict) or document.get("protocol_version") != 1:
        raise ProtocolValidationError("UNSUPPORTED_VERSION", "expected worker v1")
    validate_worker_message(document)


VALIDATORS: dict[str, Callable[[Any], None]] = {
    "events-v1.schema.json": validate_events,
    "worker-v1.schema.json": _validate_worker_v1,
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


def test_v2_refilter_worker_message_is_valid() -> None:
    validate_worker_message(
        {
            "protocol_version": 2,
            "job_id": "local-job-filter",
            "type": "start",
            "payload": {
                "operation": "refilter",
                "input_path": "C:/results/source",
                "staging_dir": "C:/results/.staging",
                "options": {
                    "filter": {
                        "format_version": 1,
                        "rules": [
                            {
                                "enabled": True,
                                "duration_ms": {"min": 100, "max": 1000},
                                "pitch": {"min": 36, "max": 84},
                            }
                        ],
                    }
                },
            },
        }
    )

    validate_worker_message(
        {
            "protocol_version": 2,
            "job_id": "local-job-auto",
            "type": "start",
            "payload": {
                "operation": "refilter",
                "input_path": "C:/results/source",
                "staging_dir": "C:/results/.staging",
                "options": {"filter_preset": "auto"},
            },
        }
    )


def test_v2_report_requires_selection_and_accepts_candidate_cache() -> None:
    report = {
        "schema_version": 2,
        "application_version": "0.1.0",
        "engine": {"name": "midi-import", "version": "mido", "backend": "python"},
        "model": None,
        "input": {
            "source_type": "midi",
            "filename": "input.mid",
            "sha256": "c" * 64,
            "segment_start_us": 0,
        },
        "parameters": {
            "filter": {
                "format_version": 1,
                "rules": [{"enabled": True, "velocity": {"min": 1, "max": 100}}],
            }
        },
        "selected_track": None,
        "elapsed_ms": 1.0,
        "counts": {
            "input_notes": 1,
            "output_notes": 1,
            "dropped_notes": 0,
            "mapped_keys": 1,
            "replaced_semitones": 0,
            "octave_folds": 0,
            "duplicate_keys": 0,
            "compatibility_collisions": 0,
        },
        "selection": {
            "format_version": 1,
            "source": "refilter",
            "spec": {
                "format_version": 1,
                "rules": [{"enabled": True, "velocity": {"min": 1, "max": 100}}],
            },
            "matched_notes": 1,
            "dropped_notes": 0,
            "rule_hits": [1],
        },
        "warnings": [],
        "artifacts": [
            {
                "kind": "candidate_cache",
                "relative_path": "score.candidates.json",
                "sha256": "d" * 64,
                "size_bytes": 10,
            }
        ],
    }
    validate_report(report)


def test_v3_worker_accepts_performance_render() -> None:
    validate_worker_message(
        {
            "protocol_version": 3,
            "job_id": "local-job-performance",
            "type": "start",
            "payload": {
                "operation": "render_performance",
                "input_path": "C:/results/source",
                "staging_dir": "C:/results/.staging",
                "options": {"title": "revision.wav", "preview_wav": True},
            },
        }
    )


def test_v3_report_accepts_performance_artifacts_and_selection() -> None:
    report = {
        "schema_version": 3,
        "application_version": "0.1.0",
        "engine": {"name": "performance-renderer", "version": "1", "backend": "python"},
        "model": None,
        "input": {
            "source_type": "midi",
            "filename": "input.mid",
            "sha256": "c" * 64,
            "segment_start_us": 0,
        },
        "parameters": {
            "performance": {
                "format_version": 1,
                "revision_id": "performance-000",
                "mapping_profile": "lyre-21-default",
                "transpose_semitones": 0,
            }
        },
        "selected_track": None,
        "elapsed_ms": 0.0,
        "counts": {
            "input_notes": 1,
            "output_notes": 1,
            "dropped_notes": 0,
            "mapped_keys": 1,
            "replaced_semitones": 0,
            "octave_folds": 0,
            "duplicate_keys": 0,
            "compatibility_collisions": 0,
        },
        "selection": {
            "format_version": 1,
            "source": "performance",
            "spec": {"format_version": 1, "rules": []},
            "matched_notes": 1,
            "dropped_notes": 0,
            "rule_hits": [1],
        },
        "warnings": [],
        "artifacts": [
            {
                "kind": "performance",
                "relative_path": "performance.json",
                "sha256": "d" * 64,
                "size_bytes": 10,
            },
            {
                "kind": "performance_midi",
                "relative_path": "performance.mid",
                "sha256": "e" * 64,
                "size_bytes": 20,
            },
        ],
    }
    validate_report(report)
