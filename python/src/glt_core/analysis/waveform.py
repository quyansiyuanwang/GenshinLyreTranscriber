"""Multi-resolution waveform peak generation."""

from __future__ import annotations

import pathlib
import struct
from dataclasses import dataclass

import numpy as np

from glt_core.analysis.decode import ANALYSIS_CHANNELS, DecodedAudio

LEVEL_BUCKET_SIZES = (64, 256, 1024, 4096)


@dataclass(frozen=True, slots=True)
class WaveformLevel:
    samples_per_bucket: int
    bucket_count: int
    offset_bytes: int
    size_bytes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "samples_per_bucket": self.samples_per_bucket,
            "bucket_count": self.bucket_count,
            "offset_bytes": self.offset_bytes,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class WaveformResult:
    path: pathlib.Path
    levels: tuple[WaveformLevel, ...]
    base_bucket_count: int


def build_waveform(
    decoded: DecodedAudio,
    destination: pathlib.Path | str,
    *,
    level_bucket_sizes: tuple[int, ...] = LEVEL_BUCKET_SIZES,
) -> WaveformResult:
    output = pathlib.Path(destination).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    source = np.memmap(
        decoded.path,
        dtype="<f4",
        mode="r",
        shape=(decoded.frames, ANALYSIS_CHANNELS),
    )
    if (
        not level_bucket_sizes
        or level_bucket_sizes != tuple(sorted(set(level_bucket_sizes)))
        or level_bucket_sizes[0] <= 0
    ):
        raise ValueError("waveform levels must be unique, increasing and positive")
    base_size = level_bucket_sizes[0]
    bucket_count = max(1, int(np.ceil(decoded.frames / base_size)))
    minimums = np.empty(bucket_count, dtype=np.float32)
    maximums = np.empty(bucket_count, dtype=np.float32)
    for bucket in range(bucket_count):
        start = bucket * base_size
        end = min(decoded.frames, start + base_size)
        if start >= end:
            minimums[bucket] = 0.0
            maximums[bucket] = 0.0
            continue
        block = np.asarray(source[start:end])
        minimums[bucket] = float(np.min(block))
        maximums[bucket] = float(np.max(block))
    del source

    level_results: list[WaveformLevel] = []
    offset = 0
    with output.open("wb") as stream:
        current_min = minimums
        current_max = maximums
        for level_index, samples_per_bucket in enumerate(level_bucket_sizes):
            if level_index > 0:
                factor = samples_per_bucket // level_bucket_sizes[level_index - 1]
                count = int(np.ceil(len(current_min) / factor))
                next_min = np.empty(count, dtype=np.float32)
                next_max = np.empty(count, dtype=np.float32)
                for index in range(count):
                    start = index * factor
                    end = min(len(current_min), start + factor)
                    next_min[index] = float(np.min(current_min[start:end]))
                    next_max[index] = float(np.max(current_max[start:end]))
                current_min, current_max = next_min, next_max
            for minimum, maximum in zip(current_min, current_max, strict=True):
                stream.write(struct.pack("<hh", _pcm16(float(minimum)), _pcm16(float(maximum))))
            size_bytes = len(current_min) * 4
            level_results.append(
                WaveformLevel(
                    samples_per_bucket=samples_per_bucket,
                    bucket_count=len(current_min),
                    offset_bytes=offset,
                    size_bytes=size_bytes,
                )
            )
            offset += size_bytes
    return WaveformResult(
        path=output,
        levels=tuple(level_results),
        base_bucket_count=bucket_count,
    )


def _pcm16(value: float) -> int:
    clipped = max(-1.0, min(1.0, value))
    return round(clipped * 32767.0)
