"""Analysis cache manifest and orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from glt_core.analysis.decode import (
    ANALYSIS_CHANNELS,
    ANALYSIS_SAMPLE_RATE,
    AnalysisCancelled,
    DecodedAudio,
    decode_audio,
)
from glt_core.analysis.spectrogram import (
    SpectralAnalysisResult,
    SpectralConfig,
    build_spectral_analysis,
)
from glt_core.analysis.waveform import WaveformResult, build_waveform
from glt_core.media.ffmpeg import MediaError

ANALYSIS_MANIFEST_NAME = "analysis-manifest-v1.json"
PCM_NAME = "analysis.pcm.f32"
WAVEFORM_NAME = "waveform.bin"


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    audio_track: int | None = None
    start_us: int | None = None
    end_us: int | None = None
    sample_rate: int = ANALYSIS_SAMPLE_RATE
    channels: int = ANALYSIS_CHANNELS
    waveform_levels: tuple[int, ...] = (64, 256, 1024, 4096)

    def validate(self) -> None:
        if self.sample_rate != ANALYSIS_SAMPLE_RATE or self.channels != ANALYSIS_CHANNELS:
            raise ValueError("analysis v1 requires 44100 Hz stereo")
        if not self.waveform_levels or self.waveform_levels != tuple(
            sorted(set(self.waveform_levels))
        ):
            raise ValueError("waveform levels must be unique and increasing")
        if self.waveform_levels[0] <= 0:
            raise ValueError("waveform levels must be positive")


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    directory: pathlib.Path
    manifest_path: pathlib.Path
    cache_key: str
    cache_hit: bool
    decoded: DecodedAudio
    waveform: WaveformResult
    spectral: SpectralAnalysisResult | None


def build_analysis_cache(
    source: pathlib.Path | str,
    directory: pathlib.Path | str,
    *,
    config: AnalysisConfig | None = None,
    spectral_config: SpectralConfig | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[str, float | None], None] | None = None,
) -> AnalysisResult:
    selected = config or AnalysisConfig()
    selected.validate()
    input_path = pathlib.Path(source).expanduser().resolve()
    output = pathlib.Path(directory).expanduser().resolve()
    if not input_path.is_file():
        raise MediaError("INPUT_NOT_FOUND", f"input media does not exist: {input_path}")
    output.mkdir(parents=True, exist_ok=True)
    source_hash = sha256_file(input_path, cancelled=cancelled)
    cache_key = _cache_key(source_hash, selected)
    manifest_path = output / ANALYSIS_MANIFEST_NAME
    pcm_path = output / PCM_NAME
    waveform_path = output / WAVEFORM_NAME
    cached = _read_cache(manifest_path, cache_key, pcm_path, waveform_path)
    if cached is not None:
        cached_result = AnalysisResult(
            directory=output,
            manifest_path=manifest_path,
            cache_key=cache_key,
            cache_hit=True,
            decoded=cached[0],
            waveform=cached[1],
            spectral=None,
        )
        if spectral_config is not None:
            spectral = build_spectral_analysis(
                cached_result.decoded,
                output,
                config=spectral_config,
                cancelled=cancelled,
                progress=(None if progress is None else lambda value: progress("features", value)),
            )
            manifest = _manifest(
                input_path,
                source_hash,
                cache_key,
                selected,
                cached_result.decoded,
                cached_result.waveform,
                spectral,
            )
            _write_json_atomic(manifest_path, manifest)
            return AnalysisResult(
                directory=cached_result.directory,
                manifest_path=cached_result.manifest_path,
                cache_key=cached_result.cache_key,
                cache_hit=True,
                decoded=cached_result.decoded,
                waveform=cached_result.waveform,
                spectral=spectral,
            )
        return cached_result

    if progress is not None:
        progress("decoding", 0.0)
    decoded = decode_audio(
        input_path,
        pcm_path,
        audio_track=selected.audio_track,
        start_us=selected.start_us,
        end_us=selected.end_us,
        cancelled=cancelled,
        progress=(None if progress is None else lambda value: progress("decoding", value)),
    )
    if progress is not None:
        progress("waveform", 0.0)
    waveform = build_waveform(
        decoded,
        waveform_path,
        level_bucket_sizes=selected.waveform_levels,
    )
    if progress is not None:
        progress("waveform", 1.0)
    new_spectral: SpectralAnalysisResult | None = None
    if spectral_config is not None:
        if progress is not None:
            progress("features", 0.0)
        new_spectral = build_spectral_analysis(
            decoded,
            output,
            config=spectral_config,
            cancelled=cancelled,
            progress=(None if progress is None else lambda value: progress("features", value)),
        )
    manifest = _manifest(
        input_path,
        source_hash,
        cache_key,
        selected,
        decoded,
        waveform,
        new_spectral,
    )
    _write_json_atomic(manifest_path, manifest)
    return AnalysisResult(
        directory=output,
        manifest_path=manifest_path,
        cache_key=cache_key,
        cache_hit=False,
        decoded=decoded,
        waveform=waveform,
        spectral=new_spectral,
    )


def sha256_file(
    path: pathlib.Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            if cancelled is not None and cancelled():
                raise AnalysisCancelled("analysis was cancelled")
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _cache_key(source_hash: str, config: AnalysisConfig) -> str:
    document = {
        "format_version": 1,
        "source_sha256": source_hash,
        "decode": {
            "audio_track": config.audio_track,
            "start_us": config.start_us,
            "end_us": config.end_us,
            "sample_rate": config.sample_rate,
            "channels": config.channels,
            "sample_format": "f32le",
        },
        "waveform_levels": list(config.waveform_levels),
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _manifest(
    source: pathlib.Path,
    source_hash: str,
    cache_key: str,
    config: AnalysisConfig,
    decoded: DecodedAudio,
    waveform: WaveformResult,
    spectral: SpectralAnalysisResult | None,
) -> dict[str, Any]:
    files = [
        _file_entry("pcm", decoded.path, decoded.sha256),
        _file_entry("waveform", waveform.path),
    ]
    document: dict[str, Any] = {
        "format_version": 1,
        "cache_key": cache_key,
        "source": {"path": str(source), "sha256": source_hash, "size_bytes": source.stat().st_size},
        "decode": {
            "audio_track": config.audio_track,
            "start_us": config.start_us,
            "end_us": config.end_us,
            "sample_rate": decoded.sample_rate,
            "channels": decoded.channels,
            "sample_format": "f32le",
            "frames": decoded.frames,
            "duration_us": decoded.duration_us,
        },
        "waveform": {
            "sample_format": "s16le-min-max-pairs",
            "base_bucket_count": waveform.base_bucket_count,
            "levels": [level.to_dict() for level in waveform.levels],
        },
        "files": files,
    }
    if spectral is not None:
        document["spectral"] = {
            "format_version": 1,
            "fft_size": spectral.spectrogram.fft_size,
            "hop_size": spectral.spectrogram.hop_size,
            "window": spectral.spectrogram.window,
            "frames": spectral.spectrogram.frames,
            "bins": spectral.spectrogram.bins,
            "db_floor": spectral.spectrogram.db_floor,
            "db_ceil": spectral.spectrogram.db_ceil,
            "spectrogram": {
                "relative_path": spectral.spectrogram.path.name,
                "sha256": spectral.spectrogram.sha256,
                "size_bytes": spectral.spectrogram.size_bytes,
            },
            "features": {
                "relative_path": spectral.features.path.name,
                "sha256": spectral.features.sha256,
                "size_bytes": spectral.features.size_bytes,
                "columns": list(spectral.features.columns),
                "hop_us": spectral.features.hop_us,
            },
        }
        files.extend(
            [
                {
                    "kind": "spectrogram",
                    "relative_path": spectral.spectrogram.path.name,
                    "sha256": spectral.spectrogram.sha256,
                    "size_bytes": spectral.spectrogram.size_bytes,
                },
                {
                    "kind": "features",
                    "relative_path": spectral.features.path.name,
                    "sha256": spectral.features.sha256,
                    "size_bytes": spectral.features.size_bytes,
                },
            ]
        )
    return document


def _file_entry(kind: str, path: pathlib.Path, digest: str | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "relative_path": path.name,
        "sha256": digest or sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _read_cache(
    manifest_path: pathlib.Path,
    cache_key: str,
    pcm_path: pathlib.Path,
    waveform_path: pathlib.Path,
) -> tuple[DecodedAudio, WaveformResult] | None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        return None
    if (
        manifest.get("cache_key") != cache_key
        or not pcm_path.is_file()
        or not waveform_path.is_file()
    ):
        return None
    decode = manifest.get("decode")
    waveform = manifest.get("waveform")
    if not isinstance(decode, dict) or not isinstance(waveform, dict):
        return None
    levels = waveform.get("levels")
    if not isinstance(levels, list):
        return None
    pcm_size = decode.get("frames")
    channels = decode.get("channels")
    if (
        not isinstance(pcm_size, int)
        or not isinstance(channels, int)
        or pcm_path.stat().st_size != pcm_size * channels * 4
    ):
        return None
    decoded = DecodedAudio(
        path=pcm_path,
        sha256=str(
            next(
                (
                    entry.get("sha256")
                    for entry in manifest.get("files", [])
                    if entry.get("kind") == "pcm"
                ),
                "",
            )
        ),
        frames=pcm_size,
        sample_rate=int(decode["sample_rate"]),
        channels=int(decode["channels"]),
        duration_us=int(decode["duration_us"]),
    )
    from glt_core.analysis.waveform import WaveformLevel

    waveform_result = WaveformResult(
        path=waveform_path,
        levels=tuple(
            WaveformLevel(
                samples_per_bucket=int(level["samples_per_bucket"]),
                bucket_count=int(level["bucket_count"]),
                offset_bytes=int(level["offset_bytes"]),
                size_bytes=int(level["size_bytes"]),
            )
            for level in levels
        ),
        base_bucket_count=int(waveform.get("base_bucket_count", 0)),
    )
    return decoded, waveform_result


def _write_json_atomic(path: pathlib.Path, document: dict[str, Any]) -> None:
    partial = path.with_name(f".{path.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        partial.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
