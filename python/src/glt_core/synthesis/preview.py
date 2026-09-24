"""Deterministic lightweight synthesis for mapped onset events."""

from __future__ import annotations

import math
import os
import pathlib
from dataclasses import dataclass
from typing import Any

import numpy as np
import soundfile

from glt_core.processing.mapping import default_mapping_layout
from glt_core.protocol.validation import validate_events

SAMPLE_RATE = 44_100
VOICE_SECONDS = 0.55
ATTACK_SECONDS = 0.003
DECAY_SECONDS = 0.32
RELEASE_SECONDS = 0.05
NOTE_AMPLITUDE = 0.28
MAX_PEAK = 0.95
_PARTIALS = ((1.0, 1.0), (2.0, 0.22), (3.0, 0.10))
_KEY_TO_PITCH = {entry.key: entry.pitch for entry in default_mapping_layout().keys}


@dataclass(frozen=True, slots=True)
class PreviewSynthesis:
    """Metadata for one written preview WAV."""

    path: pathlib.Path
    sample_rate: int
    frames: int
    peak: float


def onset_frame(at_us: int, sample_rate: int = SAMPLE_RATE) -> int:
    """Convert integer microseconds to the nearest sample frame, ties upward."""
    if at_us < 0:
        raise ValueError("onset time must be non-negative")
    if sample_rate <= 0:
        raise ValueError("sample rate must be positive")
    return (at_us * sample_rate + 500_000) // 1_000_000


def _voice(pitch: int) -> np.ndarray:
    frames = round(VOICE_SECONDS * SAMPLE_RATE)
    sample_index = np.arange(frames, dtype=np.float64)
    time = sample_index / SAMPLE_RATE
    frequency = 440.0 * (2.0 ** ((pitch - 69) / 12.0))
    weights = sum(weight for _multiple, weight in _PARTIALS)
    waveform = (
        sum(
            weight * np.sin(2.0 * math.pi * frequency * multiple * time)
            for multiple, weight in _PARTIALS
        )
        / weights
    )
    attack = np.minimum(1.0, (sample_index + 1.0) / (ATTACK_SECONDS * SAMPLE_RATE))
    decay = np.exp(-time / DECAY_SECONDS)
    release = np.minimum(
        1.0,
        (frames - sample_index) / (RELEASE_SECONDS * SAMPLE_RATE),
    )
    return np.asarray(NOTE_AMPLITUDE * attack * decay * release * waveform, dtype=np.float32)


def synthesize_preview_wav(
    events_document: dict[str, Any],
    destination: pathlib.Path | str,
    *,
    overwrite: bool = False,
) -> PreviewSynthesis:
    """Synthesize mapped onsets into a mono PCM16 preview WAV."""
    validate_events(events_document)
    events = events_document["events"]
    if not events:
        raise ValueError("empty event scores do not produce an audible preview")

    output = pathlib.Path(destination).expanduser().resolve()
    if output.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    used_pitches = {_KEY_TO_PITCH[key] for event in events for key in event["keys"]}
    voice_by_pitch = {pitch: _voice(pitch) for pitch in sorted(used_pitches)}

    duration_us = int(events_document["duration_us"])
    duration_frames = (duration_us * SAMPLE_RATE + 999_999) // 1_000_000
    last_onset_frame = max(onset_frame(int(event["at_us"])) for event in events)
    voice_frames = round(VOICE_SECONDS * SAMPLE_RATE)
    total_frames = max(duration_frames, last_onset_frame + voice_frames)
    samples = np.zeros(total_frames, dtype=np.float32)
    for event in events:
        start = onset_frame(int(event["at_us"]))
        for key in event["keys"]:
            voice = voice_by_pitch[_KEY_TO_PITCH[key]]
            samples[start : start + voice.size] += voice

    peak = float(np.max(np.abs(samples), initial=0.0))
    if peak > MAX_PEAK:
        samples *= np.float32(MAX_PEAK / peak)
        peak = MAX_PEAK

    partial = output.with_name(f".{output.stem}.partial{output.suffix}")
    partial.unlink(missing_ok=True)
    try:
        soundfile.write(partial, samples, SAMPLE_RATE, subtype="PCM_16")
        os.replace(partial, output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return PreviewSynthesis(
        path=output,
        sample_rate=SAMPLE_RATE,
        frames=total_frames,
        peak=peak,
    )
