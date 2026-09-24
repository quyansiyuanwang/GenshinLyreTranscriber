"""Human-readable and legacy-player compatibility text score exporters."""

from __future__ import annotations

import os
import pathlib
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

from glt_core.domain.note_sequence import NoteSequence
from glt_core.processing.mapping import default_mapping_layout
from glt_core.protocol.validation import validate_events

TICK_US = 10_000
_SLOTS_PER_SEGMENT = 4
_SEGMENTS_PER_LINE = 4
_SLOTS_PER_LINE = _SLOTS_PER_SEGMENT * _SEGMENTS_PER_LINE
_PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_KEY_TO_PITCH = {entry.key: entry.pitch for entry in default_mapping_layout().keys}


@dataclass(frozen=True, slots=True)
class CompatibilityScore:
    """Encoded legacy score plus representation-loss metadata."""

    text: str
    collisions: int
    represented_events: int
    omitted_tail_us: int
    max_onset_error_us: int


def _rounded_milliseconds(at_us: int) -> int:
    return (at_us + 500) // 1_000


def _format_timestamp(at_us: int) -> str:
    total_ms = _rounded_milliseconds(at_us)
    minutes, remainder_ms = divmod(total_ms, 60_000)
    seconds, milliseconds = divmod(remainder_ms, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _pitch_name(pitch: int) -> str:
    return f"{_PITCH_NAMES[pitch % 12]}{pitch // 12 - 1}"


def _key_label(key: str) -> str:
    return f"{key} ({_pitch_name(_KEY_TO_PITCH[key])})"


def _event_label(keys: list[str]) -> str:
    if len(keys) == 1:
        return _key_label(keys[0])
    key_text = " ".join(keys)
    pitch_text = " ".join(_pitch_name(_KEY_TO_PITCH[key]) for key in keys)
    return f"({key_text}) ({pitch_text})"


def build_readable_score(
    sequence: NoteSequence,
    events_document: dict[str, Any],
    *,
    title: str,
    timing_mode: str,
    transpose_semitones: int,
    mapping_profile: str,
    source_offset_us: int,
) -> str:
    """Build a human-readable onset score without pretending to be exact playback data."""
    validate_events(events_document)
    safe_title = " ".join(title.splitlines()) or "untitled"
    lines = [
        "# 人工阅读谱，不是旧播放器精确执行格式。",
        f"# 标题：{safe_title}",
        f"# 来源片段起点：{_format_timestamp(source_offset_us)}",
        f"# 总时长：{_format_timestamp(int(events_document['duration_us']))}",
        (
            f"# 时序：{timing_mode}；映射：{mapping_profile}；"
            f"实际整体移调：{transpose_semitones:+d} 半音"
        ),
        "# 图例：A (C4) 为单音；(A C E) (C4 E4 G4) 为同时和弦。",
        "# 起音时间戳之间的间隔表示休止；本谱只表达起音，不表达按住时长或连音。",
    ]
    events = events_document["events"]
    if not events:
        lines.extend(["", "## 空谱", "没有可演奏起音；时长信息保留在 JSON 或报告中。"])
        return "\n".join(lines) + "\n"

    beats = sorted(sequence.beat_grid, key=lambda point: point.at_us)
    if len(beats) >= 2:
        lines.extend(["", "## 按可靠拍点分组（未推断拍号）"])
        beat_times = [beat.at_us for beat in beats]
        grouped: dict[int, list[dict[str, Any]]] = {}
        for event in events:
            index = bisect_right(beat_times, int(event["at_us"])) - 1
            grouped.setdefault(max(index, 0), []).append(event)
        for index, beat in enumerate(beats):
            confidence = "" if beat.confidence is None else f"；置信度 {beat.confidence:.3f}"
            lines.append(
                f"### 拍 {index + 1} [{_format_timestamp(beat.at_us)}]；"
                f"BPM {beat.bpm:.3f}{confidence}"
            )
            for event in grouped.get(index, []):
                offset_ms = _rounded_milliseconds(int(event["at_us"]) - beat.at_us)
                lines.append(
                    f"[{_format_timestamp(int(event['at_us']))}] +{offset_ms}ms  "
                    f"{_event_label(list(event['keys']))}"
                )
    else:
        lines.extend(["", "## 时间轴（未使用自动拍点，不伪造小节）"])
        for event in events:
            lines.append(
                f"[{_format_timestamp(int(event['at_us']))}]  {_event_label(list(event['keys']))}"
            )

    tail_us = int(events_document["duration_us"]) - int(events[-1]["at_us"])
    if tail_us > 0:
        lines.extend(["", f"# 最后一次起音后约 {_format_timestamp(tail_us)} 为尾部静音。"])
    return "\n".join(lines) + "\n"


def build_compatibility_score(events_document: dict[str, Any]) -> CompatibilityScore:
    """Encode onsets onto the reference player's fixed 10 ms interval grid."""
    validate_events(events_document)
    grouped: dict[int, list[str]] = {}
    collision_events = 0
    max_onset_error_us = 0
    events = events_document["events"]
    for event in events:
        at_us = int(event["at_us"])
        slot = (at_us + TICK_US // 2) // TICK_US
        max_onset_error_us = max(max_onset_error_us, abs(slot * TICK_US - at_us))
        keys = list(event["keys"])
        existing = set(grouped.get(slot, ()))
        if existing.intersection(keys):
            collision_events += 1
        slot_keys = grouped.setdefault(slot, [])
        for key in keys:
            if key not in slot_keys:
                slot_keys.append(key)

    last_event_us = int(events[-1]["at_us"]) if events else 0
    omitted_tail_us = max(int(events_document["duration_us"]) - last_event_us, 0)
    if grouped:
        slots = [" "] * (max(grouped) + 1)
        for slot, keys in grouped.items():
            slots[slot] = keys[0] if len(keys) == 1 else f"({''.join(keys)})"
        while len(slots) % _SLOTS_PER_LINE:
            slots.append(" ")
        body_lines: list[str] = []
        for start in range(0, len(slots), _SLOTS_PER_LINE):
            line_slots = slots[start : start + _SLOTS_PER_LINE]
            segments = [
                "".join(line_slots[offset : offset + _SLOTS_PER_SEGMENT])
                for offset in range(0, _SLOTS_PER_LINE, _SLOTS_PER_SEGMENT)
            ]
            body_lines.append("/" + "/".join(segments) + "/")
        body = "\n".join(body_lines)
    else:
        body = "/"

    header = [
        "# GenshinLyreTranscriber 旧播放器兼容谱。",
        "# 此谱使用 10ms 近似网格；score.events.json 才是精确起音来源。",
        f"# 网格碰撞事件：{collision_events}",
        f"# 尾部静音省略：{omitted_tail_us}us",
        "# 只使用单音、同时和弦、空格休止与视觉斜杠，不使用琶音变速。",
        "version = 1.0",
        "speed_multiplier = 1.0",
        "arpeggio_interval = 0.01",
        "arpeggio_auto = true",
        f"interval_rating = {TICK_US / 1_000_000:.2f}",
        "line_interval_rating = 1.0",
        "space_interval_rating = 1.0",
        "empty_line_interval_rating = 0.0",
        "segment_length = 0",
        "segment_strict = false",
        "loop = false",
        "---",
    ]
    return CompatibilityScore(
        text="\n".join([*header, body]) + "\n",
        collisions=collision_events,
        represented_events=len(grouped),
        omitted_tail_us=omitted_tail_us,
        max_onset_error_us=max_onset_error_us,
    )


def write_text_score(
    text: str,
    destination: pathlib.Path | str,
    *,
    overwrite: bool = False,
) -> pathlib.Path:
    """Write a UTF-8 text score through a sibling partial file."""
    output = pathlib.Path(destination).expanduser().resolve()
    if output.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        partial.write_text(text, encoding="utf-8", newline="")
        if not text.endswith("\n"):
            with partial.open("a", encoding="utf-8", newline="") as stream:
                stream.write("\n")
        os.replace(partial, output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return output
