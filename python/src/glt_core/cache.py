"""Candidate note cache used for fast result-page refiltering."""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import asdict, dataclass
from typing import Any, cast

from glt_core.domain.note_sequence import (
    BeatGridPoint,
    Note,
    NoteSequence,
    Provenance,
    SourceType,
    TempoPoint,
)
from glt_core.processing.clean import CleanConfig
from glt_core.protocol import (
    ProtocolValidationError,
    validate_candidate_cache,
    validate_note_sequence,
)

CACHE_FORMAT_VERSION = 1
CANDIDATE_CACHE_NAME = "score.candidates.json"


@dataclass(frozen=True, slots=True)
class CandidateCache:
    sequence: NoteSequence
    clean_config: CleanConfig
    report_context: dict[str, Any]
    original_parameters: dict[str, Any]
    resolved_transpose: int | None
    mapping_profile: str
    format_version: int = CACHE_FORMAT_VERSION

    def validate(self) -> None:
        if self.format_version != CACHE_FORMAT_VERSION:
            raise ProtocolValidationError("UNSUPPORTED_VERSION", "unsupported candidate cache")
        self.sequence.validate()
        self.clean_config.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "format_version": self.format_version,
            "sequence": self.sequence.to_dict(),
            "clean_config": asdict(self.clean_config),
            "report_context": self.report_context,
            "original_parameters": self.original_parameters,
            "resolved_transpose": self.resolved_transpose,
            "mapping_profile": self.mapping_profile,
        }


def candidate_cache_from_dict(document: Any) -> CandidateCache:
    validate_candidate_cache(document)
    if not isinstance(document, dict):
        raise ProtocolValidationError("SCHEMA_INVALID", "candidate cache must be an object")
    required = {
        "format_version",
        "sequence",
        "clean_config",
        "report_context",
        "original_parameters",
        "resolved_transpose",
        "mapping_profile",
    }
    if set(document) != required:
        raise ProtocolValidationError("SCHEMA_INVALID", "candidate cache fields are invalid")
    version = document["format_version"]
    if isinstance(version, bool) or version != CACHE_FORMAT_VERSION:
        raise ProtocolValidationError("UNSUPPORTED_VERSION", "unsupported candidate cache")
    clean_document = document["clean_config"]
    if not isinstance(clean_document, dict) or set(clean_document) != {
        "min_confidence",
        "min_duration_us",
        "retrigger_gap_us",
    }:
        raise ProtocolValidationError("SCHEMA_INVALID", "candidate clean config is invalid")
    context = document["report_context"]
    parameters = document["original_parameters"]
    transpose = document["resolved_transpose"]
    mapping_profile = document["mapping_profile"]
    if not isinstance(context, dict) or not isinstance(parameters, dict):
        raise ProtocolValidationError("SCHEMA_INVALID", "candidate report context is invalid")
    if transpose is not None and (isinstance(transpose, bool) or not isinstance(transpose, int)):
        raise ProtocolValidationError("SCHEMA_INVALID", "resolved transpose is invalid")
    if not isinstance(mapping_profile, str):
        raise ProtocolValidationError("SCHEMA_INVALID", "mapping profile is invalid")
    cache = CandidateCache(
        sequence=_note_sequence_from_dict(document["sequence"]),
        clean_config=CleanConfig(
            min_confidence=float(clean_document["min_confidence"]),
            min_duration_us=int(clean_document["min_duration_us"]),
            retrigger_gap_us=int(clean_document["retrigger_gap_us"]),
        ),
        report_context=context,
        original_parameters=parameters,
        resolved_transpose=transpose,
        mapping_profile=mapping_profile,
    )
    cache.validate()
    return cache


def read_candidate_cache(path: pathlib.Path | str) -> CandidateCache:
    source = pathlib.Path(path).expanduser().resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProtocolValidationError("CACHE_NOT_FOUND", str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise ProtocolValidationError("CACHE_INVALID", str(exc)) from exc
    return candidate_cache_from_dict(document)


def write_candidate_cache(
    cache: CandidateCache,
    destination: pathlib.Path | str,
    *,
    overwrite: bool = False,
) -> pathlib.Path:
    output = pathlib.Path(destination).expanduser().resolve()
    if output.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        partial.write_text(
            json.dumps(cache.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(partial, output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return output


def _note_sequence_from_dict(document: Any) -> NoteSequence:
    validate_note_sequence(document)
    assert isinstance(document, dict)
    provenance_document = document["provenance"]
    assert isinstance(provenance_document, dict)
    sequence = NoteSequence(
        duration_us=int(document["duration_us"]),
        notes=tuple(
            Note(
                pitch=int(note["pitch"]),
                start_us=int(note["start_us"]),
                end_us=int(note["end_us"]),
                velocity=int(note["velocity"]),
                confidence=None if note["confidence"] is None else float(note["confidence"]),
                track=int(note["track"]),
                channel=int(note["channel"]),
            )
            for note in document["notes"]
        ),
        tempo_map=tuple(
            TempoPoint(
                at_us=int(point["at_us"]),
                bpm=float(point["bpm"]),
                source=cast(Any, point["source"]),
            )
            for point in document["tempo_map"]
        ),
        beat_grid=tuple(
            BeatGridPoint(
                at_us=int(point["at_us"]),
                beat_position=float(point["beat_position"]),
                bpm=float(point["bpm"]),
                confidence=None if point["confidence"] is None else float(point["confidence"]),
            )
            for point in document["beat_grid"]
        ),
        provenance=Provenance(
            source_type=cast(SourceType, provenance_document["source_type"]),
            source_offset_us=int(provenance_document["source_offset_us"]),
            model_version=provenance_document["model_version"],
            parameters=provenance_document["parameters"],
        ),
        schema_version=int(document["schema_version"]),
        time_unit=cast(Any, document["time_unit"]),
    )
    sequence.validate()
    return sequence
