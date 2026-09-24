"""Command-line analysis entry used by the Tauri desktop controller."""

from __future__ import annotations

import argparse
import json
import pathlib
from collections.abc import Sequence
from typing import Any

from glt_core.analysis import AnalysisConfig, SpectralConfig, build_analysis_cache
from glt_core.media.ffmpeg import MediaError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="glt-worker analyze")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--audio-track", type=int)
    parser.add_argument("--start-us", type=int)
    parser.add_argument("--end-us", type=int)
    parser.add_argument("--fft-size", type=int, default=2048)
    parser.add_argument("--hop-size", type=int, default=512)
    parser.add_argument("--window", choices=("hann", "hamming", "blackman"), default="hann")
    parser.add_argument("--pitch-min-hz", type=float, default=55.0)
    parser.add_argument("--pitch-max-hz", type=float, default=1760.0)
    parser.add_argument("--spectral", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_analysis_cache(
            pathlib.Path(args.input),
            pathlib.Path(args.output),
            config=AnalysisConfig(
                audio_track=args.audio_track,
                start_us=args.start_us,
                end_us=args.end_us,
            ),
            spectral_config=(
                SpectralConfig(
                    fft_size=args.fft_size,
                    hop_size=args.hop_size,
                    window=args.window,
                    pitch_min_hz=args.pitch_min_hz,
                    pitch_max_hz=args.pitch_max_hz,
                )
                if args.spectral
                else None
            ),
            progress=_progress,
        )
    except (MediaError, OSError, ValueError) as exc:
        _write({"type": "error", "message": str(exc)})
        return 2

    payload: dict[str, Any] = {
        "type": "result",
        "directory": str(result.directory),
        "manifest_path": str(result.manifest_path),
        "cache_key": result.cache_key,
        "cache_hit": result.cache_hit,
        "duration_us": result.decoded.duration_us,
        "frames": result.decoded.frames,
        "sample_rate": result.decoded.sample_rate,
        "channels": result.decoded.channels,
    }
    if result.spectral is not None:
        payload["spectral"] = {
            "fft_size": result.spectral.spectrogram.fft_size,
            "hop_size": result.spectral.spectrogram.hop_size,
            "window": result.spectral.spectrogram.window,
            "frames": result.spectral.spectrogram.frames,
            "bins": result.spectral.spectrogram.bins,
        }
    _write(payload)
    return 0


def _progress(stage: str, fraction: float | None) -> None:
    _write({"type": "progress", "stage": stage, "fraction": fraction})


def _write(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
