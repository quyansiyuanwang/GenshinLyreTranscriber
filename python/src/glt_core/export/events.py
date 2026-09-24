"""Exact onset-event JSON export."""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

from glt_core.domain.note_sequence import NoteSequence
from glt_core.processing.mapping import KEYBOARD_ORDER, MappedEvent
from glt_core.protocol.validation import validate_events


def build_events_document(
    sequence: NoteSequence,
    events: tuple[MappedEvent, ...],
    *,
    generator: str,
    mapping_profile: str,
) -> dict[str, Any]:
    grouped: dict[int, set[str]] = {}
    for event in events:
        grouped.setdefault(event.at_us, set()).update(event.keys)
    ordered_events = [
        {
            "at_us": at_us,
            "keys": sorted(keys, key=KEYBOARD_ORDER.index),
        }
        for at_us, keys in sorted(grouped.items())
    ]
    document = {
        "format_version": 1,
        "time_unit": "us",
        "duration_us": sequence.duration_us,
        "events": ordered_events,
        "metadata": {
            "generator": generator,
            "mapping_profile": mapping_profile,
        },
    }
    validate_events(document)
    return document


def write_events_document(
    document: dict[str, Any],
    destination: pathlib.Path | str,
    *,
    overwrite: bool = False,
) -> pathlib.Path:
    validate_events(document)
    output = pathlib.Path(destination).expanduser().resolve()
    if output.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        partial.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(partial, output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return output
