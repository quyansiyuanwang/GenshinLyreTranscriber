"""Whole-piece transpose and deterministic 21-key pitch mapping."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Literal

from glt_core.domain.note_sequence import Note, NoteSequence

KEYBOARD_ORDER = "ZXCVBNMASDFGHJQWERTYU"
NATURAL_PITCH_CLASSES = frozenset({0, 2, 4, 5, 7, 9, 11})
DEFAULT_LOW_PITCH = 48  # C3
DEFAULT_HIGH_PITCH = 83  # B5
TransposeSelection = Literal["auto"] | int


@dataclass(frozen=True, slots=True)
class KeyboardKey:
    key: str
    pitch: int


@dataclass(frozen=True, slots=True)
class MappingLayout:
    keys: tuple[KeyboardKey, ...]
    profile: str = "lyre-21-default"

    def validate(self) -> None:
        if len(self.keys) != 21:
            raise ValueError("mapping layout must contain exactly 21 keys")
        names = [entry.key for entry in self.keys]
        pitches = [entry.pitch for entry in self.keys]
        if len(set(names)) != 21 or any(name not in KEYBOARD_ORDER for name in names):
            raise ValueError("mapping keys must be unique valid lyre keys")
        if len(set(pitches)) != 21:
            raise ValueError("mapping pitches must be unique")
        for pitch in pitches:
            if (
                not DEFAULT_LOW_PITCH <= pitch <= DEFAULT_HIGH_PITCH
                or pitch % 12 not in NATURAL_PITCH_CLASSES
            ):
                raise ValueError(f"mapping pitch is outside C3-B5 naturals: {pitch}")


@dataclass(frozen=True, slots=True)
class MappingConfig:
    layout: MappingLayout
    transpose: TransposeSelection = "auto"
    tie_toward_lower: bool = True

    def validate(self) -> None:
        self.layout.validate()
        if self.transpose != "auto" and (
            isinstance(self.transpose, bool)
            or not isinstance(self.transpose, int)
            or not -48 <= self.transpose <= 48
        ):
            raise ValueError("manual transpose must be 'auto' or an integer in -48..48")


@dataclass(frozen=True, slots=True)
class MappedEvent:
    at_us: int
    keys: tuple[str, ...]
    source_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MappingStats:
    input_notes: int
    mapped_notes: int
    output_events: int
    transpose_semitones: int
    replaced_semitones: int
    octave_folds: int
    collision_notes_removed: int
    unique_keys_used: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MappingResult:
    original: NoteSequence
    mapped: NoteSequence
    events: tuple[MappedEvent, ...]
    stats: MappingStats


@dataclass(frozen=True, slots=True)
class TransposePreview:
    transpose: int
    score: tuple[float, int, int]
    selected: bool
    stats: MappingStats


@dataclass(frozen=True, slots=True)
class _Candidate:
    mapped_pitches: tuple[int, ...]
    replaced_semitones: int
    octave_folds: int
    collision_notes_removed: int
    interval_distortion: int

    def score(self, transpose: int) -> tuple[float, int, int]:
        return (
            4.0 * self.replaced_semitones
            + 1.0 * self.octave_folds
            + 4.0 * self.collision_notes_removed
            + 0.01 * self.interval_distortion,
            abs(transpose),
            transpose,
        )


def default_mapping_layout() -> MappingLayout:
    pitches = tuple(
        pitch
        for pitch in range(DEFAULT_LOW_PITCH, DEFAULT_HIGH_PITCH + 1)
        if pitch % 12 in NATURAL_PITCH_CLASSES
    )
    layout = MappingLayout(
        tuple(KeyboardKey(key, pitch) for key, pitch in zip(KEYBOARD_ORDER, pitches, strict=True))
    )
    layout.validate()
    return layout


def map_note_sequence(
    sequence: NoteSequence,
    config: MappingConfig | None = None,
) -> MappingResult:
    """Select a global transpose, map to naturals, then deduplicate same-time keys."""
    selected = config or MappingConfig(layout=default_mapping_layout())
    selected.validate()
    sequence.validate()
    transpose, candidate = _select_transpose(sequence, selected)
    mapped_pitches = candidate.mapped_pitches

    key_by_pitch = {entry.pitch: entry.key for entry in selected.layout.keys}
    order = {entry.key: index for index, entry in enumerate(selected.layout.keys)}
    grouped: dict[int, list[tuple[int, Note, int, str]]] = {}
    for index, (note, mapped_pitch) in enumerate(zip(sequence.notes, mapped_pitches, strict=True)):
        key = key_by_pitch[mapped_pitch]
        grouped.setdefault(note.start_us, []).append((index, note, mapped_pitch, key))

    events: list[MappedEvent] = []
    retained_notes: list[Note] = []
    collision_notes_removed = 0
    for at_us in sorted(grouped):
        by_key: dict[str, tuple[int, Note, int, str]] = {}
        for entry in grouped[at_us]:
            key = entry[3]
            existing = by_key.get(key)
            if existing is None or _prefer(entry, existing):
                if existing is not None:
                    collision_notes_removed += 1
                by_key[key] = entry
            else:
                collision_notes_removed += 1
        ordered = sorted(by_key.values(), key=lambda entry: order[entry[3]])
        events.append(
            MappedEvent(
                at_us=at_us,
                keys=tuple(entry[3] for entry in ordered),
                source_indices=tuple(entry[0] for entry in ordered),
            )
        )
        for _index, note, mapped_pitch, _key in ordered:
            retained_notes.append(replace(note, pitch=mapped_pitch))

    retained_notes.sort(key=lambda note: (note.start_us, note.pitch, note.track, note.channel))
    parameters = dict(sequence.provenance.parameters)
    parameters["mapping"] = {
        "profile": selected.layout.profile,
        "transpose_semitones": transpose,
        "replaced_semitones": candidate.replaced_semitones,
        "octave_folds": candidate.octave_folds,
        "collision_notes_removed": collision_notes_removed,
    }
    mapped = replace(
        sequence,
        notes=tuple(retained_notes),
        provenance=replace(sequence.provenance, parameters=parameters),
    )
    mapped.validate()
    stats = MappingStats(
        input_notes=len(sequence.notes),
        mapped_notes=len(retained_notes),
        output_events=len(events),
        transpose_semitones=transpose,
        replaced_semitones=candidate.replaced_semitones,
        octave_folds=candidate.octave_folds,
        collision_notes_removed=collision_notes_removed,
        unique_keys_used=len({key for event in events for key in event.keys}),
    )
    return MappingResult(original=sequence, mapped=mapped, events=tuple(events), stats=stats)


def preview_transpositions(
    sequence: NoteSequence,
    layout: MappingLayout | None = None,
    *,
    transposes: tuple[int, ...] | None = None,
) -> tuple[TransposePreview, ...]:
    """Return all deterministic transpose candidates ordered by mapping score."""
    sequence.validate()
    selected_layout = layout or default_mapping_layout()
    selected_layout.validate()
    candidates = transposes or tuple(range(-12, 13))
    if any(isinstance(value, bool) or not isinstance(value, int) for value in candidates):
        raise ValueError("transpose candidates must be integers")
    config = MappingConfig(layout=selected_layout)
    evaluated = [(transpose, _evaluate(sequence, config, transpose)) for transpose in candidates]
    if not evaluated:
        return ()
    best_transpose = min(evaluated, key=lambda item: item[1].score(item[0]))[0]
    ordered = sorted(
        evaluated,
        key=lambda item: (item[1].score(item[0]), item[0]),
    )
    return tuple(
        TransposePreview(
            transpose=transpose,
            score=candidate.score(transpose),
            selected=transpose == best_transpose,
            stats=_candidate_stats(sequence, candidate, transpose),
        )
        for transpose, candidate in ordered
    )


def _candidate_stats(
    sequence: NoteSequence,
    candidate: _Candidate,
    transpose: int,
) -> MappingStats:
    return MappingStats(
        input_notes=len(sequence.notes),
        mapped_notes=len(sequence.notes) - candidate.collision_notes_removed,
        output_events=len({note.start_us for note in sequence.notes}),
        transpose_semitones=transpose,
        replaced_semitones=candidate.replaced_semitones,
        octave_folds=candidate.octave_folds,
        collision_notes_removed=candidate.collision_notes_removed,
        unique_keys_used=len(set(candidate.mapped_pitches)),
    )


def _select_transpose(sequence: NoteSequence, config: MappingConfig) -> tuple[int, _Candidate]:
    if isinstance(config.transpose, int):
        return config.transpose, _evaluate(sequence, config, config.transpose)
    candidates: list[tuple[tuple[float, int, int], int, _Candidate]] = []
    for transpose in range(-12, 13):
        candidate = _evaluate(sequence, config, transpose)
        candidates.append((candidate.score(transpose), transpose, candidate))
    _score, transpose, candidate = min(candidates, key=lambda item: item[0])
    return transpose, candidate


def _evaluate(sequence: NoteSequence, config: MappingConfig, transpose: int) -> _Candidate:
    mapped: list[int] = []
    replaced = 0
    folds = 0
    for note in sequence.notes:
        transposed = note.pitch + transpose
        natural = _nearest_natural(transposed, config.tie_toward_lower)
        if natural != transposed:
            replaced += 1
        folded = _fold_into_range(natural)
        if folded != natural:
            folds += abs(folded - natural) // 12
        mapped.append(folded)
    collisions = _collision_count(sequence.notes, mapped)
    distortion = _interval_distortion(sequence.notes, mapped)
    return _Candidate(
        mapped_pitches=tuple(mapped),
        replaced_semitones=replaced,
        octave_folds=folds,
        collision_notes_removed=collisions,
        interval_distortion=distortion,
    )


def _nearest_natural(pitch: int, tie_toward_lower: bool) -> int:
    if pitch % 12 in NATURAL_PITCH_CLASSES:
        return pitch
    lower = pitch
    while lower % 12 not in NATURAL_PITCH_CLASSES:
        lower -= 1
    upper = pitch
    while upper % 12 not in NATURAL_PITCH_CLASSES:
        upper += 1
    lower_distance = pitch - lower
    upper_distance = upper - pitch
    if lower_distance < upper_distance:
        return lower
    if upper_distance < lower_distance:
        return upper
    return lower if tie_toward_lower else upper


def _fold_into_range(pitch: int) -> int:
    while pitch < DEFAULT_LOW_PITCH:
        pitch += 12
    while pitch > DEFAULT_HIGH_PITCH:
        pitch -= 12
    return pitch


def _collision_count(notes: tuple[Note, ...], mapped: list[int]) -> int:
    grouped: dict[int, set[int]] = {}
    for note, pitch in zip(notes, mapped, strict=True):
        grouped.setdefault(note.start_us, set()).add(pitch)
    return sum(max(0, len(grouped[at_us]) - 1) for at_us in grouped)


def _interval_distortion(notes: tuple[Note, ...], mapped: list[int]) -> int:
    ordered = sorted(zip(notes, mapped, strict=True), key=lambda item: item[0].start_us)
    distortion = 0
    for (left_note, left_mapped), (right_note, right_mapped) in zip(
        ordered, ordered[1:], strict=False
    ):
        if left_note.start_us == right_note.start_us:
            continue
        distortion += abs((right_note.pitch - left_note.pitch) - (right_mapped - left_mapped))
    return distortion


def _prefer(
    candidate: tuple[int, Note, int, str],
    existing: tuple[int, Note, int, str],
) -> bool:
    candidate_note = candidate[1]
    existing_note = existing[1]
    return (
        candidate_note.velocity,
        candidate_note.confidence if candidate_note.confidence is not None else -1.0,
        -candidate[2],
        -candidate[0],
    ) > (
        existing_note.velocity,
        existing_note.confidence if existing_note.confidence is not None else -1.0,
        -existing[2],
        -existing[0],
    )
