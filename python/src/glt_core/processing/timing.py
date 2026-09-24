"""Beat candidates, local tempo estimation and confidence-based fallback."""

from __future__ import annotations

import math
import pathlib
from dataclasses import dataclass, replace

import librosa
import numpy as np

from glt_core.domain.note_sequence import BeatGridPoint, NoteSequence, TempoPoint

DEFAULT_SAMPLE_RATE = 22_050
DEFAULT_HOP_LENGTH = 512


class TimingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TimingConfig:
    min_bpm: float = 40.0
    max_bpm: float = 240.0
    min_beats: int = 4
    confidence_threshold: float = 0.2
    hop_length: int = DEFAULT_HOP_LENGTH

    def validate(self) -> None:
        if not 0 < self.min_bpm < self.max_bpm <= 1000:
            raise ValueError("BPM range is invalid")
        if self.min_beats < 2:
            raise ValueError("min_beats must be at least 2")
        if not 0 <= self.confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be in range 0..1")
        if self.hop_length <= 0:
            raise ValueError("hop_length must be positive")


@dataclass(frozen=True, slots=True)
class TimingAnalysis:
    sequence: NoteSequence
    estimated_bpm: float | None
    confidence: float
    fallback: bool
    reason: str
    beat_count: int


def analyze_timing(
    audio_path: pathlib.Path | str | None,
    sequence: NoteSequence,
    *,
    config: TimingConfig | None = None,
) -> TimingAnalysis:
    """Estimate beats for audio, or preserve an existing MIDI tempo map."""
    selected = config or TimingConfig()
    selected.validate()
    sequence.validate()
    if sequence.provenance.source_type == "midi":
        return TimingAnalysis(
            sequence=sequence,
            estimated_bpm=None,
            confidence=1.0,
            fallback=False,
            reason="preserve_midi",
            beat_count=len(sequence.beat_grid),
        )
    if audio_path is None:
        return _fallback(sequence, 0.0, "missing_audio", 0)
    source = pathlib.Path(audio_path).expanduser().resolve()
    if not source.is_file():
        raise TimingError("INPUT_NOT_FOUND", f"audio does not exist: {source}")
    try:
        samples, sample_rate = librosa.load(str(source), sr=DEFAULT_SAMPLE_RATE, mono=True)
    except (OSError, ValueError) as exc:
        raise TimingError("AUDIO_DECODE_FAILED", str(exc)) from exc
    if samples.size == 0:
        return _fallback(sequence, 0.0, "empty_audio", 0)
    onset_envelope = librosa.onset.onset_strength(
        y=samples,
        sr=sample_rate,
        hop_length=selected.hop_length,
    )
    if onset_envelope.size == 0 or float(np.max(onset_envelope)) <= 0:
        return _fallback(sequence, 0.0, "no_onsets", 0)
    _tempo, detected_beats = librosa.beat.beat_track(
        onset_envelope=onset_envelope,
        sr=sample_rate,
        hop_length=selected.hop_length,
        trim=False,
    )
    beat_frames = np.asarray(detected_beats, dtype=int)
    if beat_frames.size < selected.min_beats:
        return _fallback(sequence, 0.0, "too_few_beats", int(beat_frames.size))
    beat_times = librosa.frames_to_time(
        beat_frames,
        sr=sample_rate,
        hop_length=selected.hop_length,
    )
    intervals = np.diff(beat_times)
    if intervals.size == 0 or not np.all(np.isfinite(intervals)) or np.any(intervals <= 0):
        return _fallback(sequence, 0.0, "invalid_intervals", int(beat_frames.size))
    beat_bpm = np.asarray([_normalize_bpm(60.0 / interval, selected) for interval in intervals])
    estimated_bpm = float(np.median(beat_bpm))
    if not selected.min_bpm <= estimated_bpm <= selected.max_bpm:
        return _fallback(sequence, 0.0, "bpm_out_of_range", int(beat_frames.size))
    normalized_strength = onset_envelope / float(np.max(onset_envelope))
    strengths = normalized_strength[np.clip(beat_frames, 0, normalized_strength.size - 1)]
    median_interval = float(np.median(intervals))
    regularity = 1.0 / (1.0 + float(np.std(intervals)) / median_interval)
    confidence = float(np.clip(0.55 * np.mean(strengths) + 0.30 * regularity + 0.15, 0.0, 1.0))
    if not math.isfinite(confidence) or confidence < selected.confidence_threshold:
        return _fallback(sequence, confidence, "low_confidence", int(beat_frames.size))

    local_bpm: list[float] = []
    for index in range(beat_frames.size):
        if index == 0:
            local_bpm.append(float(beat_bpm[0]))
        elif index == beat_frames.size - 1:
            local_bpm.append(float(beat_bpm[-1]))
        else:
            local_bpm.append(float(120.0 / (intervals[index - 1] + intervals[index])))
    tempo_map = tuple(
        TempoPoint(
            at_us=round(float(time_value) * 1_000_000),
            bpm=bpm,
            source="estimated",
        )
        for time_value, bpm in zip(beat_times, local_bpm, strict=True)
    )
    beat_grid = tuple(
        BeatGridPoint(
            at_us=round(float(time_value) * 1_000_000),
            beat_position=float(index),
            bpm=bpm,
            confidence=float(strength),
        )
        for index, (time_value, bpm, strength) in enumerate(
            zip(beat_times, local_bpm, strengths, strict=True)
        )
    )
    parameters = dict(sequence.provenance.parameters)
    parameters["timing"] = {
        "estimated_bpm": estimated_bpm,
        "confidence": confidence,
        "beat_count": len(beat_grid),
        "fallback": False,
    }
    estimated = replace(
        sequence,
        tempo_map=tempo_map,
        beat_grid=beat_grid,
        provenance=replace(sequence.provenance, parameters=parameters),
    )
    estimated.validate()
    return TimingAnalysis(
        sequence=estimated,
        estimated_bpm=estimated_bpm,
        confidence=confidence,
        fallback=False,
        reason="estimated",
        beat_count=len(beat_grid),
    )


def _fallback(
    sequence: NoteSequence,
    confidence: float,
    reason: str,
    beat_count: int,
) -> TimingAnalysis:
    parameters = dict(sequence.provenance.parameters)
    parameters["timing"] = {
        "confidence": confidence,
        "beat_count": beat_count,
        "fallback": True,
        "reason": reason,
    }
    preserved = replace(
        sequence,
        provenance=replace(sequence.provenance, parameters=parameters),
    )
    return TimingAnalysis(
        sequence=preserved,
        estimated_bpm=None,
        confidence=confidence,
        fallback=True,
        reason=reason,
        beat_count=beat_count,
    )


def _normalize_bpm(bpm: float, config: TimingConfig) -> float:
    while bpm < config.min_bpm:
        bpm *= 2.0
    while bpm > config.max_bpm:
        bpm /= 2.0
    return bpm
