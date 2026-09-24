"""Chunked STFT and time-frequency feature extraction."""

from __future__ import annotations

import hashlib
import math
import os
import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np

from glt_core.analysis.decode import AnalysisCancelled, DecodedAudio

WindowName = Literal["hann", "hamming", "blackman"]

SPECTROGRAM_NAME = "spectrogram.bin"
FEATURES_NAME = "features.bin"
FEATURE_COLUMNS = (
    "rms",
    "peak",
    "centroid_hz",
    "rolloff_hz",
    "flatness",
    "flux",
    "zcr",
    "onset_strength",
    "pitch_hz",
    "pitch_confidence",
    *tuple(f"chroma_{index}" for index in range(12)),
)


@dataclass(frozen=True, slots=True)
class SpectralConfig:
    fft_size: int = 2048
    hop_size: int = 512
    window: WindowName = "hann"
    db_floor: float = -120.0
    db_ceil: float = 0.0
    pitch_min_hz: float = 55.0
    pitch_max_hz: float = 1760.0
    rolloff: float = 0.85

    def validate(self) -> None:
        if self.fft_size < 64 or self.fft_size & (self.fft_size - 1):
            raise ValueError("fft_size must be a power of two and at least 64")
        if self.hop_size <= 0 or self.hop_size > self.fft_size:
            raise ValueError("hop_size must be in range 1..fft_size")
        if self.window not in {"hann", "hamming", "blackman"}:
            raise ValueError("unsupported window")
        if not -200.0 <= self.db_floor < self.db_ceil <= 30.0:
            raise ValueError("invalid spectrogram dB range")
        if not 20.0 <= self.pitch_min_hz < self.pitch_max_hz:
            raise ValueError("invalid pitch range")
        if not 0.5 <= self.rolloff < 1.0:
            raise ValueError("rolloff must be in range 0.5..1.0")


@dataclass(frozen=True, slots=True)
class SpectrogramResult:
    path: pathlib.Path
    sha256: str
    size_bytes: int
    frames: int
    bins: int
    fft_size: int
    hop_size: int
    window: WindowName
    db_floor: float
    db_ceil: float


@dataclass(frozen=True, slots=True)
class FeatureResult:
    path: pathlib.Path
    sha256: str
    size_bytes: int
    frames: int
    columns: tuple[str, ...]
    hop_us: int


@dataclass(frozen=True, slots=True)
class SpectralAnalysisResult:
    spectrogram: SpectrogramResult
    features: FeatureResult


def build_spectral_analysis(
    decoded: DecodedAudio,
    directory: pathlib.Path | str,
    *,
    config: SpectralConfig | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[float | None], None] | None = None,
) -> SpectralAnalysisResult:
    selected = config or SpectralConfig()
    selected.validate()
    output_dir = pathlib.Path(directory).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    spectrogram_path = output_dir / SPECTROGRAM_NAME
    features_path = output_dir / FEATURES_NAME
    temporary_spectrogram = output_dir / f".{SPECTROGRAM_NAME}.f16.partial"
    temporary_features = output_dir / f".{FEATURES_NAME}.partial"
    temporary_spectrogram.unlink(missing_ok=True)
    temporary_features.unlink(missing_ok=True)

    frame_count = max(1, 1 + max(0, decoded.frames - selected.fft_size) // selected.hop_size)
    bins = selected.fft_size // 2 + 1
    spectrogram_temp = np.memmap(
        temporary_spectrogram,
        dtype="<f2",
        mode="w+",
        shape=(frame_count, bins),
    )
    feature_array = np.memmap(
        temporary_features,
        dtype="<f4",
        mode="w+",
        shape=(frame_count, len(FEATURE_COLUMNS)),
    )
    pcm = np.memmap(
        decoded.path,
        dtype="<f4",
        mode="r",
        shape=(decoded.frames, decoded.channels),
    )
    window = _window(selected.window, selected.fft_size)
    frequencies = np.fft.rfftfreq(selected.fft_size, 1.0 / decoded.sample_rate)
    chroma_filter = _chroma_filter(frequencies)
    previous_magnitude: np.ndarray | None = None
    block_frames = 2048
    try:
        for block_start in range(0, frame_count, block_frames):
            if cancelled is not None and cancelled():
                raise AnalysisCancelled("analysis was cancelled")
            block_end = min(frame_count, block_start + block_frames)
            count = block_end - block_start
            first_sample = block_start * selected.hop_size
            sample_end = (block_end - 1) * selected.hop_size + selected.fft_size
            block = np.asarray(pcm[first_sample:sample_end]).mean(axis=1)
            if block.size < selected.fft_size:
                block = np.pad(block, (0, selected.fft_size - block.size))
            frames = np.lib.stride_tricks.sliding_window_view(block, selected.fft_size)[
                : count * selected.hop_size : selected.hop_size
            ][:count]
            windowed = frames * window
            magnitude = np.abs(np.fft.rfft(windowed, axis=1))
            magnitude_scale = magnitude / np.maximum(magnitude.max(axis=1, keepdims=True), 1e-12)
            db = np.maximum(20.0 * np.log10(np.maximum(magnitude, 1e-12)), selected.db_floor)
            spectrogram_temp[block_start:block_end] = db.astype(np.float16)

            total_energy = np.maximum(np.sum(magnitude, axis=1), 1e-12)
            centroid = np.sum(magnitude * frequencies, axis=1) / total_energy
            cumulative = np.cumsum(magnitude, axis=1)
            rolloff_index = np.argmax(
                cumulative >= (selected.rolloff * total_energy[:, None]),
                axis=1,
            )
            flatness = np.exp(np.mean(np.log(np.maximum(magnitude, 1e-12)), axis=1)) / (
                np.mean(magnitude, axis=1) + 1e-12
            )
            flux = np.zeros(count, dtype=np.float32)
            if count > 1:
                within = np.diff(magnitude_scale, axis=0)
                flux[1:] = np.maximum(within, 0.0).mean(axis=1)
            if previous_magnitude is not None:
                flux[0] = float(np.maximum(magnitude_scale[0] - previous_magnitude, 0.0).mean())
            previous_magnitude = magnitude_scale[-1].astype(np.float32, copy=True)
            chroma = magnitude @ chroma_filter.T
            chroma /= np.maximum(chroma.sum(axis=1, keepdims=True), 1e-12)
            pitch_hz, pitch_confidence = _autocorrelation_pitch(
                frames,
                decoded.sample_rate,
                selected.pitch_min_hz,
                selected.pitch_max_hz,
            )
            rms = np.sqrt(np.mean(np.square(frames), axis=1))
            peak = np.max(np.abs(frames), axis=1)
            zcr = np.mean(np.abs(np.diff(np.signbit(frames), axis=1)), axis=1)
            feature_array[block_start:block_end, 0] = rms
            feature_array[block_start:block_end, 1] = peak
            feature_array[block_start:block_end, 2] = centroid
            feature_array[block_start:block_end, 3] = frequencies[rolloff_index]
            feature_array[block_start:block_end, 4] = flatness
            feature_array[block_start:block_end, 5] = flux
            feature_array[block_start:block_end, 6] = zcr
            feature_array[block_start:block_end, 7] = flux
            feature_array[block_start:block_end, 8] = pitch_hz
            feature_array[block_start:block_end, 9] = pitch_confidence
            feature_array[block_start:block_end, 10:] = chroma
            if progress is not None:
                progress((block_end / frame_count) if frame_count else None)
        spectrogram_temp.flush()
        feature_array.flush()
    except BaseException:
        del spectrogram_temp
        del feature_array
        temporary_spectrogram.unlink(missing_ok=True)
        temporary_features.unlink(missing_ok=True)
        raise
    finally:
        del pcm
    del spectrogram_temp
    del feature_array

    _quantize_spectrogram(
        temporary_spectrogram,
        spectrogram_path,
        shape=(frame_count, bins),
        floor=selected.db_floor,
        ceiling=selected.db_ceil,
    )
    os.replace(temporary_features, features_path)
    spectrogram_temp_path = temporary_spectrogram
    spectrogram_temp_path.unlink(missing_ok=True)
    return SpectralAnalysisResult(
        spectrogram=SpectrogramResult(
            path=spectrogram_path,
            sha256=sha256_file(spectrogram_path),
            size_bytes=spectrogram_path.stat().st_size,
            frames=frame_count,
            bins=bins,
            fft_size=selected.fft_size,
            hop_size=selected.hop_size,
            window=selected.window,
            db_floor=selected.db_floor,
            db_ceil=selected.db_ceil,
        ),
        features=FeatureResult(
            path=features_path,
            sha256=sha256_file(features_path),
            size_bytes=features_path.stat().st_size,
            frames=frame_count,
            columns=FEATURE_COLUMNS,
            hop_us=round(selected.hop_size * 1_000_000 / decoded.sample_rate),
        ),
    )


def sha256_file(path: pathlib.Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _window(name: WindowName, size: int) -> np.ndarray:
    if name == "hann":
        return np.hanning(size).astype(np.float32)
    if name == "hamming":
        return np.hamming(size).astype(np.float32)
    return np.blackman(size).astype(np.float32)


def _chroma_filter(frequencies: np.ndarray) -> np.ndarray:
    result = np.zeros((12, len(frequencies)), dtype=np.float32)
    active = (frequencies >= 55.0) & (frequencies <= 5000.0)
    midi = np.rint(69.0 + 12.0 * np.log2(np.maximum(frequencies, 1e-12) / 440.0)).astype(int)
    for bin_index in np.flatnonzero(active):
        result[midi[bin_index] % 12, bin_index] = 1.0
    return result


def _autocorrelation_pitch(
    frames: np.ndarray,
    sample_rate: int,
    minimum_hz: float,
    maximum_hz: float,
) -> tuple[np.ndarray, np.ndarray]:
    centered = frames - np.mean(frames, axis=1, keepdims=True)
    fft_size = 1 << int(math.ceil(math.log2(max(2, centered.shape[1] * 2))))
    spectrum = np.fft.rfft(centered, n=fft_size, axis=1)
    autocorrelation = np.fft.irfft(np.abs(spectrum) ** 2, n=fft_size, axis=1)[
        :, : centered.shape[1]
    ]
    minimum_lag = max(1, int(sample_rate / maximum_hz))
    maximum_lag = min(centered.shape[1] - 1, int(sample_rate / minimum_hz))
    if maximum_lag <= minimum_lag:
        zeros = np.zeros(centered.shape[0], dtype=np.float32)
        return zeros, zeros
    window = autocorrelation[:, minimum_lag : maximum_lag + 1]
    lags = np.argmax(window, axis=1) + minimum_lag
    peak = window[np.arange(window.shape[0]), lags - minimum_lag]
    confidence = np.clip(peak / np.maximum(autocorrelation[:, 0], 1e-12), 0.0, 1.0)
    pitch = sample_rate / np.maximum(lags, 1)
    pitch[confidence < 0.05] = 0.0
    return pitch.astype(np.float32), confidence.astype(np.float32)


def _quantize_spectrogram(
    source: pathlib.Path,
    destination: pathlib.Path,
    *,
    shape: tuple[int, int],
    floor: float,
    ceiling: float,
) -> None:
    partial = destination.with_name(f".{destination.name}.partial")
    partial.unlink(missing_ok=True)
    data = np.memmap(source, dtype="<f2", mode="r", shape=shape)
    output = np.memmap(partial, dtype=np.uint8, mode="w+", shape=shape)
    block_rows = 4096
    try:
        for start in range(0, shape[0], block_rows):
            end = min(shape[0], start + block_rows)
            normalized = (np.asarray(data[start:end], dtype=np.float32) - floor) / (ceiling - floor)
            output[start:end] = np.rint(np.clip(normalized, 0.0, 1.0) * 255.0).astype(np.uint8)
        output.flush()
    except BaseException:
        del output
        del data
        partial.unlink(missing_ok=True)
        raise
    else:
        del output
        del data
    os.replace(partial, destination)
