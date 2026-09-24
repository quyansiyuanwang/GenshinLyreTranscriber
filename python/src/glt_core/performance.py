"""Editable performance document and deterministic player-facing exports."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

import pretty_midi

from glt_core.analysis.notes import AnalysisNote, BendClass, PitchBendPoint
from glt_core.domain.note_sequence import (
    BeatGridPoint,
    Note,
    NoteSequence,
    Provenance,
    SourceType,
    TempoPoint,
)
from glt_core.export.events import write_events_document
from glt_core.export.text import (
    CompatibilityScore,
    build_compatibility_score,
    build_readable_score,
    write_text_score,
)
from glt_core.processing.mapping import KEYBOARD_ORDER, MappingResult, default_mapping_layout
from glt_core.protocol.validation import validate_performance
from glt_core.synthesis.preview import synthesize_preview_wav

PERFORMANCE_FORMAT_VERSION = 1
PERFORMANCE_NAME = "performance.json"
PERFORMANCE_MIDI_NAME = "performance.mid"
PERFORMANCE_REVISION = "performance-000"
EVENTS_NAME = "score.events.json"
READABLE_NAME = "score.readable.txt"
COMPAT_NAME = "score.compat.txt"
PREVIEW_NAME = "preview.wav"

RevisionSource = Literal["transcribe", "convert_midi", "refilter", "render_performance", "edit"]


@dataclass(frozen=True, slots=True)
class PerformanceRevision:
    id: str
    parent_id: str | None
    source: RevisionSource

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "parent_id": self.parent_id, "source": self.source}


@dataclass(frozen=True, slots=True)
class PerformanceSource:
    type: SourceType
    offset_us: int

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "offset_us": self.offset_us}


@dataclass(frozen=True, slots=True)
class PerformanceMapping:
    profile: str
    transpose_semitones: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "transpose_semitones": self.transpose_semitones,
        }


@dataclass(frozen=True, slots=True)
class PerformanceNote:
    id: str
    start_us: int
    end_us: int
    key: str
    pitch: int
    velocity: int
    confidence: float | None
    source_stem: str
    candidate_id: str | None
    original_pitch: int
    pitch_center: float
    pitch_bend_class: BendClass
    pitch_bends: tuple[PitchBendPoint, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "key": self.key,
            "pitch": self.pitch,
            "velocity": self.velocity,
            "confidence": self.confidence,
            "source_stem": self.source_stem,
            "candidate_id": self.candidate_id,
            "original_pitch": self.original_pitch,
            "pitch_center": self.pitch_center,
            "pitch_bend_class": self.pitch_bend_class,
            "pitch_bends": [point.to_dict() for point in self.pitch_bends],
        }


@dataclass(frozen=True, slots=True)
class PerformanceDocument:
    duration_us: int
    revision: PerformanceRevision
    source: PerformanceSource
    mapping: PerformanceMapping
    tempo_map: tuple[TempoPoint, ...]
    beat_grid: tuple[BeatGridPoint, ...]
    notes: tuple[PerformanceNote, ...]
    format_version: int = PERFORMANCE_FORMAT_VERSION
    time_unit: Literal["us"] = "us"

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "time_unit": self.time_unit,
            "duration_us": self.duration_us,
            "revision": self.revision.to_dict(),
            "source": self.source.to_dict(),
            "mapping": self.mapping.to_dict(),
            "tempo_map": [
                {
                    "at_us": point.at_us,
                    "bpm": point.bpm,
                    "source": point.source,
                }
                for point in self.tempo_map
            ],
            "beat_grid": [
                {
                    "at_us": point.at_us,
                    "beat_position": point.beat_position,
                    "bpm": point.bpm,
                    "confidence": point.confidence,
                }
                for point in self.beat_grid
            ],
            "notes": [note.to_dict() for note in self.notes],
        }

    def validate(self) -> None:
        validate_performance(self.to_dict())


@dataclass(frozen=True, slots=True)
class PerformanceBundle:
    performance_path: pathlib.Path
    midi_path: pathlib.Path
    events_path: pathlib.Path
    readable_path: pathlib.Path
    compatibility_path: pathlib.Path
    preview_path: pathlib.Path | None
    sequence: NoteSequence
    events_document: dict[str, Any]
    compatibility: CompatibilityScore
    artifacts: tuple[dict[str, Any], ...]


def build_performance(
    mapping: MappingResult,
    *,
    source_type: SourceType,
    revision_source: RevisionSource,
    source_offset_us: int,
    source_stem: str = "mix",
    analysis_notes: tuple[AnalysisNote, ...] = (),
) -> PerformanceDocument:
    """Build the editable performance from the final routed and mapped sequence."""
    mapping.original.validate()
    if source_offset_us < 0:
        raise ValueError("source offset must be non-negative")
    if not source_stem:
        raise ValueError("source stem must not be empty")
    profile = str(mapping.mapped.provenance.parameters.get("mapping", {}).get("profile", "unknown"))
    mapping_document = mapping.mapped.provenance.parameters.get("mapping")
    if not isinstance(mapping_document, dict):
        raise ValueError("mapped sequence is missing mapping provenance")
    transpose = int(mapping_document.get("transpose_semitones", 0))
    default_pitches = {entry.key: entry.pitch for entry in default_mapping_layout().keys}
    analysis_by_reference: dict[tuple[int, int, int], AnalysisNote] = {}
    for analysis_note in sorted(analysis_notes, key=lambda value: value.id):
        analysis_by_reference.setdefault(
            (analysis_note.start_us, analysis_note.end_us, analysis_note.pitch),
            analysis_note,
        )

    notes: list[PerformanceNote] = []
    for event in mapping.events:
        if len(event.keys) != len(event.source_indices):
            raise ValueError("mapping event key/source index count does not match")
        for key, source_index in zip(event.keys, event.source_indices, strict=True):
            if not 0 <= source_index < len(mapping.original.notes):
                raise ValueError("mapping event contains an invalid source index")
            original = mapping.original.notes[source_index]
            analysis = analysis_by_reference.get(
                (original.start_us, original.end_us, original.pitch)
            )
            pitch = default_pitches[key]
            center = (
                analysis.pitch_center if analysis is not None else float(original.pitch + transpose)
            )
            notes.append(
                PerformanceNote(
                    id="pending",
                    start_us=original.start_us,
                    end_us=original.end_us,
                    key=key,
                    pitch=pitch,
                    velocity=original.velocity,
                    confidence=original.confidence,
                    source_stem=analysis.source_stem if analysis is not None else source_stem,
                    candidate_id=analysis.id if analysis is not None else None,
                    original_pitch=original.pitch,
                    pitch_center=center,
                    pitch_bend_class=(
                        analysis.pitch_bend_class if analysis is not None else "stable"
                    ),
                    pitch_bends=analysis.pitch_bends if analysis is not None else (),
                )
            )
    notes.sort(key=lambda note: (note.start_us, note.pitch, note.key))
    identified = tuple(
        replace(note, id=f"perf-{index:06d}") for index, note in enumerate(notes, start=1)
    )
    document = PerformanceDocument(
        duration_us=mapping.original.duration_us,
        revision=PerformanceRevision(PERFORMANCE_REVISION, None, revision_source),
        source=PerformanceSource(source_type, source_offset_us),
        mapping=PerformanceMapping(profile, transpose),
        tempo_map=mapping.original.tempo_map,
        beat_grid=mapping.original.beat_grid,
        notes=identified,
    )
    document.validate()
    return document


def performance_from_dict(document: Any) -> PerformanceDocument:
    validate_performance(document)
    assert isinstance(document, dict)
    revision = document["revision"]
    source = document["source"]
    mapping = document["mapping"]
    return PerformanceDocument(
        duration_us=int(document["duration_us"]),
        revision=PerformanceRevision(
            id=str(revision["id"]),
            parent_id=None if revision["parent_id"] is None else str(revision["parent_id"]),
            source=cast(RevisionSource, revision["source"]),
        ),
        source=PerformanceSource(
            type=cast(SourceType, source["type"]),
            offset_us=int(source["offset_us"]),
        ),
        mapping=PerformanceMapping(
            profile=str(mapping["profile"]),
            transpose_semitones=int(mapping["transpose_semitones"]),
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
        notes=tuple(
            PerformanceNote(
                id=str(note["id"]),
                start_us=int(note["start_us"]),
                end_us=int(note["end_us"]),
                key=str(note["key"]),
                pitch=int(note["pitch"]),
                velocity=int(note["velocity"]),
                confidence=None if note["confidence"] is None else float(note["confidence"]),
                source_stem=str(note["source_stem"]),
                candidate_id=None if note["candidate_id"] is None else str(note["candidate_id"]),
                original_pitch=int(note["original_pitch"]),
                pitch_center=float(note["pitch_center"]),
                pitch_bend_class=cast(BendClass, note["pitch_bend_class"]),
                pitch_bends=tuple(
                    PitchBendPoint(at_us=int(point["at_us"]), cents=float(point["cents"]))
                    for point in note["pitch_bends"]
                ),
            )
            for note in document["notes"]
        ),
        format_version=int(document["format_version"]),
        time_unit=cast(Any, document["time_unit"]),
    )


def read_performance(path: pathlib.Path | str) -> PerformanceDocument:
    source = pathlib.Path(path).expanduser().resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read performance file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"performance JSON is invalid: {exc}") from exc
    return performance_from_dict(document)


def performance_to_note_sequence(document: PerformanceDocument) -> NoteSequence:
    document.validate()
    sequence = NoteSequence(
        duration_us=document.duration_us,
        notes=tuple(
            Note(
                pitch=note.pitch,
                start_us=note.start_us,
                end_us=note.end_us,
                velocity=note.velocity,
                confidence=note.confidence,
                track=0,
                channel=0,
            )
            for note in document.notes
        ),
        tempo_map=document.tempo_map,
        beat_grid=document.beat_grid,
        provenance=Provenance(
            source_type=document.source.type,
            source_offset_us=document.source.offset_us,
            model_version=None,
            parameters={
                "performance": {
                    "format_version": document.format_version,
                    "revision_id": document.revision.id,
                    "mapping_profile": document.mapping.profile,
                    "transpose_semitones": document.mapping.transpose_semitones,
                }
            },
        ),
    )
    sequence.validate()
    return sequence


def performance_to_events_document(
    document: PerformanceDocument,
    *,
    generator: str,
) -> dict[str, Any]:
    document.validate()
    grouped: dict[int, set[str]] = {}
    for note in document.notes:
        grouped.setdefault(note.start_us, set()).add(note.key)
    events = [
        {
            "at_us": at_us,
            "keys": sorted(keys, key=KEYBOARD_ORDER.index),
        }
        for at_us, keys in sorted(grouped.items())
    ]
    result = {
        "format_version": 1,
        "time_unit": "us",
        "duration_us": document.duration_us,
        "events": events,
        "metadata": {
            "generator": generator,
            "mapping_profile": document.mapping.profile,
        },
    }
    from glt_core.protocol.validation import validate_events

    validate_events(result)
    return result


def write_performance(
    document: PerformanceDocument,
    destination: pathlib.Path | str,
) -> pathlib.Path:
    document.validate()
    output = pathlib.Path(destination).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        partial.write_text(
            json.dumps(document.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(partial, output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return output


def write_performance_midi(
    document: PerformanceDocument,
    destination: pathlib.Path | str,
) -> pathlib.Path:
    document.validate()
    output = pathlib.Path(destination).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial")
    partial.unlink(missing_ok=True)
    midi = pretty_midi.PrettyMIDI(initial_tempo=120.0, resolution=960)
    instrument = pretty_midi.Instrument(program=0, name="GenshinLyreTranscriber Performance")
    midi.instruments.append(instrument)
    for note in document.notes:
        instrument.notes.append(
            pretty_midi.Note(
                velocity=note.velocity,
                pitch=note.pitch,
                start=note.start_us / 1_000_000,
                end=note.end_us / 1_000_000,
            )
        )
    try:
        midi.write(str(partial))
        os.replace(partial, output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return output


def render_performance_bundle(
    document: PerformanceDocument,
    output_dir: pathlib.Path | str,
    *,
    title: str,
    timing_mode: str,
    preview_wav: bool,
    generator: str,
) -> PerformanceBundle:
    """Write all player-facing artifacts from one immutable performance document."""
    document.validate()
    output = pathlib.Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    sequence = performance_to_note_sequence(document)
    events_document = performance_to_events_document(document, generator=generator)

    performance_path = write_performance(document, output / PERFORMANCE_NAME)
    midi_path = write_performance_midi(document, output / PERFORMANCE_MIDI_NAME)
    events_path = write_events_document(
        events_document,
        output / EVENTS_NAME,
        overwrite=True,
    )
    readable_text = build_readable_score(
        sequence,
        events_document,
        title=title,
        timing_mode=timing_mode,
        transpose_semitones=document.mapping.transpose_semitones,
        mapping_profile=document.mapping.profile,
        source_offset_us=document.source.offset_us,
    )
    readable_path = write_text_score(readable_text, output / READABLE_NAME, overwrite=True)
    compatibility = build_compatibility_score(events_document)
    compatibility_path = write_text_score(
        compatibility.text,
        output / COMPAT_NAME,
        overwrite=True,
    )
    preview_path: pathlib.Path | None = None
    if preview_wav and events_document["events"]:
        preview_path = synthesize_preview_wav(
            events_document,
            output / PREVIEW_NAME,
            overwrite=True,
        ).path

    artifacts: list[dict[str, Any]] = [
        _artifact("performance", performance_path),
        _artifact("performance_midi", midi_path),
        _artifact("events", events_path),
        _artifact("readable_text", readable_path),
        _artifact("compat_text", compatibility_path),
    ]
    if preview_path is not None:
        artifacts.append(_artifact("preview_wav", preview_path))
    return PerformanceBundle(
        performance_path=performance_path,
        midi_path=midi_path,
        events_path=events_path,
        readable_path=readable_path,
        compatibility_path=compatibility_path,
        preview_path=preview_path,
        sequence=sequence,
        events_document=events_document,
        compatibility=compatibility,
        artifacts=tuple(artifacts),
    )


def _artifact(kind: str, path: pathlib.Path) -> dict[str, Any]:
    return {
        "kind": kind,
        "relative_path": path.name,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
