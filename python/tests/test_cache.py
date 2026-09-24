from __future__ import annotations

import pytest

from glt_core.cache import CandidateCache, candidate_cache_from_dict
from glt_core.domain.note_sequence import BeatGridPoint, Note, NoteSequence, Provenance, TempoPoint
from glt_core.processing.clean import CleanConfig
from glt_core.protocol import ProtocolValidationError


def test_candidate_cache_round_trip_preserves_confidence_and_timing() -> None:
    sequence = NoteSequence(
        duration_us=1_000_000,
        notes=(Note(60, 100_000, 500_000, 80, 0.73, 0, 0),),
        tempo_map=(TempoPoint(0, 120.0, "estimated"),),
        beat_grid=(BeatGridPoint(0, 0.0, 120.0, 0.8),),
        provenance=Provenance("audio", 0, "nmp.onnx", {"source": "test"}),
    )
    cache = CandidateCache(
        sequence=sequence,
        clean_config=CleanConfig(),
        report_context={"input": {"filename": "song.flac"}},
        original_parameters={"timing": "auto", "transpose": "auto"},
        resolved_transpose=1,
        mapping_profile="lyre-21-default",
    )
    restored = candidate_cache_from_dict(cache.to_dict())
    assert restored.sequence == sequence
    assert restored.clean_config == cache.clean_config
    assert restored.resolved_transpose == 1


def test_candidate_cache_rejects_unknown_fields() -> None:
    document = {
        "format_version": 1,
        "sequence": {},
        "clean_config": {},
        "report_context": {},
        "original_parameters": {},
        "resolved_transpose": None,
        "mapping_profile": "lyre-21-default",
        "unknown": True,
    }
    with pytest.raises(ProtocolValidationError):
        candidate_cache_from_dict(document)
