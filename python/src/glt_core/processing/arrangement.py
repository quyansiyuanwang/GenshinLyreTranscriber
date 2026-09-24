"""Convert dense note streams into playable lyre onset groups."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from glt_core.domain.note_sequence import Note, NoteSequence


@dataclass(frozen=True, slots=True)
class ArrangementConfig:
    enabled: bool = True
    onset_window_us: int = 150_000
    max_voices: int = 2

    def validate(self) -> None:
        if self.onset_window_us <= 0:
            raise ValueError("onset_window_us must be positive")
        if not 1 <= self.max_voices <= 21:
            raise ValueError("max_voices must be in range 1..21")


@dataclass(frozen=True, slots=True)
class ArrangementStats:
    input_notes: int
    output_notes: int
    dropped_notes: int
    input_events: int
    output_events: int
    reduced_events: int
    max_voices: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ArrangementResult:
    original: NoteSequence
    arranged: NoteSequence
    stats: ArrangementStats


def arrange_note_sequence(
    sequence: NoteSequence,
    config: ArrangementConfig | None = None,
) -> ArrangementResult:
    """Keep a small, complementary chord at each near-simultaneous onset."""
    selected = config or ArrangementConfig()
    selected.validate()
    sequence.validate()
    if not selected.enabled:
        return ArrangementResult(
            original=sequence,
            arranged=sequence,
            stats=_stats(sequence, sequence, max_voices=selected.max_voices),
        )

    ordered = sorted(
        enumerate(sequence.notes),
        key=lambda item: (item[1].start_us, item[1].pitch, item[1].track, item[0]),
    )
    arranged: list[Note] = []
    reduced_events = 0
    index = 0
    while index < len(ordered):
        anchor_us = ordered[index][1].start_us
        end = index + 1
        while (
            end < len(ordered) and ordered[end][1].start_us - anchor_us <= selected.onset_window_us
        ):
            end += 1
        cluster = [note for _source_index, note in ordered[index:end]]
        chosen = _select_voices(cluster, selected.max_voices)
        if len(cluster) > len(chosen):
            reduced_events += 1
        for note in chosen:
            shift = anchor_us - note.start_us
            end_us = min(sequence.duration_us, max(anchor_us + 1, note.end_us + shift))
            arranged.append(replace(note, start_us=anchor_us, end_us=end_us))
        index = end

    arranged.sort(key=lambda note: (note.start_us, note.pitch, note.track, note.channel))
    parameters = dict(sequence.provenance.parameters)
    parameters["arrangement"] = {
        "enabled": True,
        "onset_window_us": selected.onset_window_us,
        "max_voices": selected.max_voices,
    }
    result = replace(
        sequence,
        notes=tuple(arranged),
        provenance=replace(sequence.provenance, parameters=parameters),
    )
    result.validate()
    return ArrangementResult(
        original=sequence,
        arranged=result,
        stats=_stats(
            sequence,
            result,
            max_voices=selected.max_voices,
            reduced_events=reduced_events,
        ),
    )


def _select_voices(candidates: list[Note], max_voices: int) -> list[Note]:
    if len(candidates) <= max_voices:
        return list(candidates)
    remaining = list(candidates)
    chosen = [max(remaining, key=_primary_score)]
    remaining.remove(chosen[0])
    while remaining and len(chosen) < max_voices:
        next_note = max(remaining, key=lambda note: _secondary_score(note, chosen))
        chosen.append(next_note)
        remaining.remove(next_note)
    return chosen


def _primary_score(note: Note) -> tuple[float, int, int]:
    confidence = note.confidence if note.confidence is not None else 0.0
    duration = min((note.end_us - note.start_us) / 1_000_000, 1.0)
    return (
        note.velocity / 127 + 0.15 * duration + 0.05 * confidence,
        -note.pitch,
        -note.start_us,
    )


def _secondary_score(note: Note, chosen: list[Note]) -> tuple[float, int, int]:
    salience = _primary_score(note)[0]
    penalty = 0.0
    for existing in chosen:
        distance = abs(note.pitch - existing.pitch)
        if distance == 0:
            penalty += 2.0
        elif distance % 12 == 0:
            penalty += 0.45
        if note.pitch % 12 == existing.pitch % 12:
            penalty += 0.35
        penalty += 0.025 * abs(distance - 8)
    return (salience - penalty, note.velocity, -note.pitch)


def _stats(
    original: NoteSequence,
    arranged: NoteSequence,
    *,
    max_voices: int,
    reduced_events: int = 0,
) -> ArrangementStats:
    input_events = len({note.start_us for note in original.notes})
    output_events = len({note.start_us for note in arranged.notes})
    return ArrangementStats(
        input_notes=len(original.notes),
        output_notes=len(arranged.notes),
        dropped_notes=max(0, len(original.notes) - len(arranged.notes)),
        input_events=input_events,
        output_events=output_events,
        reduced_events=reduced_events,
        max_voices=max_voices,
    )
