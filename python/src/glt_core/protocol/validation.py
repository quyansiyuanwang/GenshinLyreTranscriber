"""Schema and semantic validation shared by core modules and contract tests."""

from __future__ import annotations

import json
import os
import pathlib
import sys
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator

SAFE_INTEGER_MAX = 9_007_199_254_740_991
SCHEMA_FILES = {
    "analysis_manifest": "analysis-manifest-v1.schema.json",
    "separator_component": "separator-component-v1.schema.json",
    "routing_plan": "routing-plan-v1.schema.json",
    "notes": "notes-v1.schema.json",
    "stem_set": "stem-set-v1.schema.json",
    "events": "events-v1.schema.json",
    "worker": "worker-v2.schema.json",
    "note_sequence": "note-sequence-v1.schema.json",
    "report": "report-v2.schema.json",
    "candidate_cache": "candidate-cache-v1.schema.json",
}


class ProtocolValidationError(ValueError):
    """A stable protocol rejection with an optional JSON pointer."""

    def __init__(self, code: str, message: str, pointer: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.pointer = pointer


class _DuplicateKeyError(ValueError):
    pass


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite number: {value}")


def parse_json_text(text: str) -> Any:
    """Parse strict protocol JSON, rejecting duplicate keys and non-finite numbers."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, _DuplicateKeyError, ValueError) as exc:
        raise ProtocolValidationError("INVALID_JSON", str(exc)) from exc


def _schema_directory() -> pathlib.Path:
    configured = os.environ.get("GLT_SCHEMA_DIR")
    if configured:
        return pathlib.Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return pathlib.Path(getattr(sys, "_MEIPASS", "")) / "glt_core" / "schemas"
    return pathlib.Path(__file__).resolve().parents[4] / "schemas"


@cache
def _validator(schema_file: str) -> Draft202012Validator:
    schema_path = _schema_directory() / schema_file
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load protocol schema {schema_path}: {exc}") from exc
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _schema_error(document: Any, schema_file: str) -> None:
    errors = sorted(
        _validator(schema_file).iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.path),
    )
    if not errors:
        return
    error = errors[0]
    pointer = (
        "/" + "/".join(str(part) for part in error.absolute_path) if error.absolute_path else None
    )
    raise ProtocolValidationError("SCHEMA_INVALID", error.message, pointer)


def validate_schema(document: Any, schema_file: str) -> None:
    """Validate a document against one named or explicitly referenced schema file."""
    selected = SCHEMA_FILES.get(schema_file, schema_file)
    _schema_error(document, selected)


def _require_version(document: Any, allowed: set[int] | None = None) -> None:
    if not isinstance(document, dict):
        return
    value = document.get(
        "format_version",
        document.get("protocol_version", document.get("schema_version")),
    )
    versions = allowed or {1}
    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or value not in versions
    ):
        raise ProtocolValidationError("UNSUPPORTED_VERSION", f"unsupported version: {value!r}")


def _validate_safe_time(value: Any, pointer: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= SAFE_INTEGER_MAX:
        raise ProtocolValidationError(
            "SCHEMA_INVALID", "expected a safe non-negative integer", pointer
        )
    return value


def validate_events(document: Any) -> None:
    """Validate an events document, including ordering and duration semantics."""
    _require_version(document)
    _schema_error(document, SCHEMA_FILES["events"])
    assert isinstance(document, dict)
    duration_us = int(document["duration_us"])
    previous_at: int | None = None
    for index, event in enumerate(document["events"]):
        at_us = int(event["at_us"])
        if at_us > duration_us:
            raise ProtocolValidationError(
                "DURATION_RANGE", "event is after duration_us", f"/events/{index}/at_us"
            )
        if previous_at is not None and at_us <= previous_at:
            raise ProtocolValidationError(
                "EVENT_ORDER",
                "event times must be strictly increasing",
                f"/events/{index}/at_us",
            )
        previous_at = at_us


def validate_worker_message(document: Any) -> None:
    """Validate one worker JSONL message at the schema boundary."""
    _require_version(document, {1, 2})
    version = document.get("protocol_version") if isinstance(document, dict) else None
    schema = "worker-v1.schema.json" if version == 1 else SCHEMA_FILES["worker"]
    _schema_error(document, schema)


def validate_note_sequence(document: Any) -> None:
    """Validate a NoteSequence document, ordering and note ranges."""
    _require_version(document)
    _schema_error(document, SCHEMA_FILES["note_sequence"])
    assert isinstance(document, dict)
    duration_us = int(document["duration_us"])
    previous: tuple[int, int, int] | None = None
    for index, note in enumerate(document["notes"]):
        start_us = _validate_safe_time(note["start_us"], f"/notes/{index}/start_us")
        end_us = _validate_safe_time(note["end_us"], f"/notes/{index}/end_us")
        if not start_us < end_us <= duration_us:
            raise ProtocolValidationError(
                "NOTE_RANGE",
                "note must satisfy start_us < end_us <= duration_us",
                f"/notes/{index}",
            )
        order = (start_us, int(note["pitch"]), int(note["track"]))
        if previous is not None and order < previous:
            raise ProtocolValidationError(
                "NOTE_ORDER",
                "notes are not sorted by start_us, pitch and track",
                f"/notes/{index}",
            )
        previous = order

    tempo_times = [int(point["at_us"]) for point in document["tempo_map"]]
    if tempo_times != sorted(tempo_times):
        raise ProtocolValidationError(
            "TEMPO_ORDER", "tempo_map must be ordered by at_us", "/tempo_map"
        )
    beat_times = [int(point["at_us"]) for point in document["beat_grid"]]
    if beat_times != sorted(beat_times):
        raise ProtocolValidationError(
            "BEAT_ORDER", "beat_grid must be ordered by at_us", "/beat_grid"
        )


def _validate_relative_path(value: str, pointer: str) -> None:
    normalized = value.replace("\\", "/")
    components = normalized.split("/")
    if (
        normalized.startswith("/")
        or (len(normalized) >= 2 and normalized[1] == ":")
        or any(part in {"", ".", ".."} for part in components)
    ):
        raise ProtocolValidationError(
            "UNSAFE_PATH",
            "artifact path must be relative and cannot escape the result directory",
            pointer,
        )


def validate_report(document: Any) -> None:
    """Validate a report document and its relative artifact paths."""
    _require_version(document, {1, 2})
    version = document.get("schema_version") if isinstance(document, dict) else None
    schema = "report-v1.schema.json" if version == 1 else SCHEMA_FILES["report"]
    _schema_error(document, schema)
    assert isinstance(document, dict)
    for index, artifact in enumerate(document["artifacts"]):
        _validate_relative_path(str(artifact["relative_path"]), f"/artifacts/{index}/relative_path")


def validate_candidate_cache(document: Any) -> None:
    """Validate the internal candidate cache used by worker refiltering."""
    _require_version(document)
    _schema_error(document, SCHEMA_FILES["candidate_cache"])


def validate_analysis_manifest(document: Any) -> None:
    """Validate the canonical PCM and waveform cache manifest."""
    _require_version(document)
    _schema_error(document, SCHEMA_FILES["analysis_manifest"])
    assert isinstance(document, dict)
    for index, artifact in enumerate(document["files"]):
        _validate_relative_path(
            str(artifact["relative_path"]),
            f"/files/{index}/relative_path",
        )


def validate_separator_component(document: Any) -> None:
    """Validate an optional separator component manifest."""
    _require_version(document)
    _schema_error(document, SCHEMA_FILES["separator_component"])


def validate_stem_set(document: Any) -> None:
    """Validate a separated stem set document."""
    _require_version(document)
    _schema_error(document, SCHEMA_FILES["stem_set"])


def validate_routing_plan(document: Any) -> None:
    """Validate a stem and performance routing plan."""
    _require_version(document)
    _schema_error(document, SCHEMA_FILES["routing_plan"])


def validate_analysis_notes(document: Any) -> None:
    """Validate analysis notes, pitch bends and semantic time bounds."""
    _require_version(document)
    _schema_error(document, SCHEMA_FILES["notes"])
    assert isinstance(document, dict)
    duration_us = int(document["duration_us"])
    previous: tuple[int, int, str] | None = None
    for index, note in enumerate(document["notes"]):
        start_us = int(note["start_us"])
        end_us = int(note["end_us"])
        if not start_us < end_us <= duration_us:
            raise ProtocolValidationError(
                "NOTE_RANGE",
                "analysis note must satisfy start_us < end_us <= duration_us",
                f"/notes/{index}",
            )
        order = (start_us, int(note["pitch"]), str(note["id"]))
        if previous is not None and order < previous:
            raise ProtocolValidationError("NOTE_ORDER", "analysis notes are not sorted")
        previous = order
