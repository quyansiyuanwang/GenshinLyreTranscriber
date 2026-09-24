from __future__ import annotations

from glt_core.domain.note_sequence import Note, NoteSequence, Provenance
from glt_core.processing.auto_filter import detect_filter_spec, filter_preset


def _note(
    pitch: int,
    start_us: int,
    *,
    duration_us: int,
    velocity: int,
    confidence: float | None,
) -> Note:
    return Note(pitch, start_us, start_us + duration_us, velocity, confidence, 0, 0)


def _sequence() -> NoteSequence:
    notes: list[Note] = []
    for index in range(40):
        notes.append(
            _note(
                60 + index % 7,
                index * 250_000,
                duration_us=400_000,
                velocity=55,
                confidence=0.6,
            )
        )
    for index in range(15):
        notes.append(
            _note(
                36 + index % 3,
                300_000 + index * 700_000,
                duration_us=80_000,
                velocity=90,
                confidence=0.25,
            )
        )
    sequence = NoteSequence(
        duration_us=max(note.end_us for note in notes),
        notes=tuple(sorted(notes, key=lambda note: (note.start_us, note.pitch))),
        tempo_map=(),
        beat_grid=(),
        provenance=Provenance("audio", 0, "test", {}),
    )
    sequence.validate()
    return sequence


def test_auto_filter_generates_editable_rules_and_diagnostics() -> None:
    recommendation = detect_filter_spec(_sequence())
    assert recommendation.spec.rules
    assert recommendation.diagnostics["preset"] == "auto"
    assert recommendation.diagnostics["suspected_notes"] > 0
    assert recommendation.diagnostics["used_confidence"]


def test_auto_filter_falls_back_for_small_input() -> None:
    sequence = _sequence()
    small = NoteSequence(
        duration_us=max(note.end_us for note in sequence.notes[:3]),
        notes=sequence.notes[:3],
        tempo_map=(),
        beat_grid=(),
        provenance=sequence.provenance,
    )
    recommendation = detect_filter_spec(small)
    assert recommendation.spec.rules == ()
    assert recommendation.diagnostics["fallback"] == "too_few_notes"


def test_named_presets_are_valid() -> None:
    for name in ("off", "balanced", "melody", "auto"):
        recommendation = filter_preset(name, _sequence())
        recommendation.spec.validate()
