from __future__ import annotations

import pathlib

from glt_core.domain.note_sequence import BeatGridPoint, NoteSequence, Provenance
from glt_core.export import (
    build_compatibility_score,
    build_readable_score,
    write_text_score,
)


def _sequence(beat_grid: tuple[BeatGridPoint, ...] = ()) -> NoteSequence:
    return NoteSequence(
        duration_us=2_000_000,
        notes=(),
        tempo_map=(),
        beat_grid=beat_grid,
        provenance=Provenance("audio", 0, "test", {}),
    )


def _document(events: list[dict[str, object]], duration_us: int = 2_000_000) -> dict[str, object]:
    return {
        "format_version": 1,
        "time_unit": "us",
        "duration_us": duration_us,
        "events": events,
        "metadata": {"generator": "test", "mapping_profile": "lyre-21-default"},
    }


def test_compatibility_score_keeps_leading_rest_and_adjacent_events() -> None:
    score = build_compatibility_score(
        _document(
            [
                {"at_us": 5_000, "keys": ["A"]},
                {"at_us": 25_000, "keys": ["C", "E"]},
            ]
        )
    )

    assert score.max_onset_error_us == 5_000
    assert score.collisions == 0
    body = "\n".join(line for line in score.text.splitlines() if line.startswith("/"))
    assert body.replace("/", "").rstrip() == " A (CE)"
    assert body.count("/") == 5
    assert "interval_rating = 0.01" in score.text
    assert "line_interval_rating = 1.0" in score.text


def test_compatibility_score_reports_same_slot_key_collision() -> None:
    score = build_compatibility_score(
        _document(
            [
                {"at_us": 10_000, "keys": ["A"]},
                {"at_us": 14_999, "keys": ["A", "C"]},
            ]
        )
    )

    assert score.collisions == 1
    assert score.represented_events == 1
    body = "\n".join(line for line in score.text.splitlines() if line.startswith("/"))
    assert body.replace("/", "").rstrip() == " (AC)"
    assert "# 网格碰撞事件：1" in score.text


def test_empty_compatibility_score_has_no_note_body() -> None:
    score = build_compatibility_score(_document([], duration_us=1_234_567))

    assert score.text.splitlines()[-1] == "/"
    assert score.omitted_tail_us == 1_234_567
    assert score.max_onset_error_us == 0


def test_readable_score_marks_its_role_and_uses_reliable_beats() -> None:
    document = _document(
        [
            {"at_us": 0, "keys": ["A"]},
            {"at_us": 500_000, "keys": ["A", "D"]},
        ]
    )
    sequence = _sequence(
        (
            BeatGridPoint(0, 0.0, 120.0, 0.8),
            BeatGridPoint(500_000, 1.0, 120.0, 0.7),
        )
    )

    text = build_readable_score(
        sequence,
        document,
        title="sample.mid",
        timing_mode="preserve",
        transpose_semitones=-1,
        mapping_profile="lyre-21-default",
        source_offset_us=250_000,
    )

    assert text.splitlines()[0] == "# 人工阅读谱，不是旧播放器精确执行格式。"
    assert "按可靠拍点分组（未推断拍号）" in text
    assert "拍 1 [00:00.000]" in text
    assert "A (C4)" in text
    assert "(A D) (C4 E4)" in text
    assert "实际整体移调：-1 半音" in text
    assert "来源片段起点：00:00.250" in text


def test_readable_empty_score_keeps_duration_context() -> None:
    text = build_readable_score(
        _sequence(),
        _document([], duration_us=1_234_567),
        title="silence.wav",
        timing_mode="auto",
        transpose_semitones=0,
        mapping_profile="lyre-21-default",
        source_offset_us=0,
    )

    assert "## 空谱" in text
    assert "总时长：00:01.235" in text


def test_write_text_score_uses_utf8_and_rejects_implicit_overwrite(tmp_path: pathlib.Path) -> None:
    output = tmp_path / "score.txt"
    write_text_score("A\n", output)
    assert output.read_text(encoding="utf-8") == "A\n"
    try:
        write_text_score("B\n", output)
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing output was overwritten without permission")
    assert output.read_text(encoding="utf-8") == "A\n"
