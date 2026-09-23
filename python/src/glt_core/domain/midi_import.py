"""Mido MIDI import and exact tick-to-microsecond conversion."""

from __future__ import annotations

import os
import pathlib
import shutil
from collections import defaultdict, deque
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import mido

from glt_core.domain.note_sequence import Note, NoteSequence, Provenance, TempoPoint
from glt_core.media.ffmpeg import sha256_file

DEFAULT_TEMPO_US_PER_BEAT = 500_000
PERCUSSION_CHANNEL = 9


class MidiImportError(RuntimeError):
    """A stable MIDI import failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class MidiWarning:
    code: str
    message: str
    count: int


@dataclass(frozen=True, slots=True)
class MidiImportResult:
    sequence: NoteSequence
    warnings: tuple[MidiWarning, ...]
    source_format: int
    source_sha256: str


@dataclass(frozen=True, slots=True)
class _TempoEvent:
    tick: int
    tempo_us_per_beat: int
    track: int
    order: int


@dataclass(frozen=True, slots=True)
class _PendingNote:
    start_tick: int
    velocity: int


class _TickConverter:
    def __init__(self, ticks_per_beat: int, tempo_events: list[_TempoEvent]) -> None:
        self._ticks_per_beat = ticks_per_beat
        self._tempo_events = sorted(
            tempo_events, key=lambda event: (event.tick, event.track, event.order)
        )
        self._cache: dict[int, int] = {0: 0}

    def microseconds(self, tick: int) -> int:
        cached = self._cache.get(tick)
        if cached is not None:
            return cached
        previous_tick = 0
        current_tempo = DEFAULT_TEMPO_US_PER_BEAT
        elapsed = Fraction(0)
        for event in self._tempo_events:
            if event.tick > tick:
                break
            elapsed += Fraction((event.tick - previous_tick) * current_tempo, self._ticks_per_beat)
            previous_tick = event.tick
            current_tempo = event.tempo_us_per_beat
        elapsed += Fraction((tick - previous_tick) * current_tempo, self._ticks_per_beat)
        value = round(elapsed)
        self._cache[tick] = value
        return value


def import_midi(path: pathlib.Path | str) -> MidiImportResult:
    """Import format 0/1 MIDI into a validated NoteSequence."""
    source = pathlib.Path(path).expanduser().resolve()
    if not source.is_file():
        raise MidiImportError("INPUT_NOT_FOUND", f"MIDI file does not exist: {source}")
    source_hash = sha256_file(source)
    try:
        midi = mido.MidiFile(str(source), clip=False)
    except (OSError, EOFError, ValueError) as exc:
        raise MidiImportError("INVALID_MIDI", str(exc)) from exc
    if midi.type not in {0, 1}:
        raise MidiImportError("UNSUPPORTED_FORMAT", f"MIDI format {midi.type} is not supported")
    if (
        not isinstance(midi.ticks_per_beat, int)
        or isinstance(midi.ticks_per_beat, bool)
        or midi.ticks_per_beat <= 0
    ):
        raise MidiImportError(
            "UNSUPPORTED_TIMEBASE", "SMPTE or invalid MIDI division is not supported"
        )

    tempo_events: list[_TempoEvent] = []
    note_messages: list[tuple[int, int, int, Any]] = []
    track_end_ticks: list[int] = []
    for track_index, track in enumerate(midi.tracks):
        absolute_tick = 0
        for order, message in enumerate(track):
            absolute_tick += int(message.time)
            if message.type == "set_tempo":
                tempo_events.append(
                    _TempoEvent(absolute_tick, int(message.tempo), track_index, order)
                )
            elif message.type in {"note_on", "note_off"}:
                note_messages.append((absolute_tick, track_index, order, message))
        track_end_ticks.append(absolute_tick)

    converter = _TickConverter(midi.ticks_per_beat, tempo_events)
    pending: dict[tuple[int, int, int], deque[_PendingNote]] = defaultdict(deque)
    notes: list[Note] = []
    warning_counts: dict[str, int] = defaultdict(int)
    for absolute_tick, track_index, _order, message in note_messages:
        channel = int(message.channel)
        pitch = int(message.note)
        if channel == PERCUSSION_CHANNEL:
            if message.type == "note_on" and int(message.velocity) > 0:
                warning_counts["PERCUSSION_FILTERED"] += 1
            continue
        key = (track_index, channel, pitch)
        is_note_on = message.type == "note_on" and int(message.velocity) > 0
        if is_note_on:
            pending[key].append(_PendingNote(absolute_tick, int(message.velocity)))
            continue
        if not pending[key]:
            warning_counts["ORPHAN_NOTE_OFF"] += 1
            continue
        started = pending[key].popleft()
        note = _build_note(
            started,
            absolute_tick,
            track_index,
            channel,
            pitch,
            converter,
            warning_counts,
        )
        if note is not None:
            notes.append(note)

    for (track_index, channel, pitch), queue in pending.items():
        track_end_tick = track_end_ticks[track_index] if track_index < len(track_end_ticks) else 0
        for started in queue:
            end_tick = max(track_end_tick, started.start_tick + 1)
            warning_counts["MISSING_NOTE_OFF"] += 1
            note = _build_note(
                started,
                end_tick,
                track_index,
                channel,
                pitch,
                converter,
                warning_counts,
            )
            if note is not None:
                notes.append(note)

    notes.sort(key=lambda note: (note.start_us, note.pitch, note.track))
    duration_us = max((note.end_us for note in notes), default=0)
    tempo_map = tuple(
        TempoPoint(
            at_us=converter.microseconds(event.tick),
            bpm=60_000_000 / event.tempo_us_per_beat,
            source="midi",
        )
        for event in sorted(tempo_events, key=lambda event: (event.tick, event.track, event.order))
    )
    sequence = NoteSequence(
        duration_us=duration_us,
        notes=tuple(notes),
        tempo_map=tempo_map,
        beat_grid=(),
        provenance=Provenance(
            source_type="midi",
            source_offset_us=0,
            model_version=None,
            parameters={
                "midi_format": midi.type,
                "ticks_per_beat": midi.ticks_per_beat,
            },
        ),
    )
    sequence.validate()
    if sha256_file(source) != source_hash:
        raise MidiImportError("SOURCE_CHANGED", "MIDI source changed during import")
    warnings = tuple(
        MidiWarning(code=code, message=_warning_message(code), count=count)
        for code, count in sorted(warning_counts.items())
        if count > 0
    )
    return MidiImportResult(
        sequence=sequence,
        warnings=warnings,
        source_format=midi.type,
        source_sha256=source_hash,
    )


def copy_source_midi(
    source: pathlib.Path | str,
    destination: pathlib.Path | str,
    *,
    overwrite: bool = False,
) -> pathlib.Path:
    """Copy an imported MIDI atomically without modifying the source."""
    source_path = pathlib.Path(source).expanduser().resolve()
    destination_path = pathlib.Path(destination).expanduser().resolve()
    if not source_path.is_file():
        raise MidiImportError("INPUT_NOT_FOUND", f"MIDI file does not exist: {source_path}")
    if destination_path.exists() and not overwrite:
        raise MidiImportError("OUTPUT_EXISTS", f"output already exists: {destination_path}")
    source_hash = sha256_file(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    partial = destination_path.with_name(f".{destination_path.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        shutil.copyfile(source_path, partial)
        if sha256_file(partial) != source_hash:
            raise MidiImportError("COPY_FAILED", "copied MIDI does not match the source")
        os.replace(partial, destination_path)
    except MidiImportError:
        partial.unlink(missing_ok=True)
        raise
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise MidiImportError("OUTPUT_FAILED", f"cannot copy MIDI: {exc}") from exc
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if sha256_file(source_path) != source_hash:
        raise MidiImportError("SOURCE_CHANGED", "MIDI source changed during copy")
    return destination_path


def _build_note(
    started: _PendingNote,
    end_tick: int,
    track: int,
    channel: int,
    pitch: int,
    converter: _TickConverter,
    warning_counts: dict[str, int],
) -> Note | None:
    start_us = converter.microseconds(started.start_tick)
    end_us = converter.microseconds(end_tick)
    if end_us <= start_us:
        warning_counts["ZERO_LENGTH_NOTE"] += 1
        end_us = start_us + 1
    if not 1 <= started.velocity <= 127:
        warning_counts["INVALID_VELOCITY"] += 1
        return None
    return Note(
        pitch=pitch,
        start_us=start_us,
        end_us=end_us,
        velocity=started.velocity,
        confidence=None,
        track=track,
        channel=channel,
    )


def _warning_message(code: str) -> str:
    messages = {
        "PERCUSSION_FILTERED": "percussion note-ons were filtered from channel 10",
        "ORPHAN_NOTE_OFF": "note-off messages without a matching note-on were ignored",
        "MISSING_NOTE_OFF": "open notes were closed at the track end",
        "ZERO_LENGTH_NOTE": "zero-length repairs were extended by one tick",
        "INVALID_VELOCITY": "notes with invalid velocity were rejected",
    }
    return messages.get(code, code)
