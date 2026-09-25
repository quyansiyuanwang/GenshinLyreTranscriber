"""Streaming decode to the canonical analysis PCM format."""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import soundfile
from scipy.signal import resample_poly

from glt_core.media.ffmpeg import AudioStream, MediaError, probe_media, resolve_ffmpeg_tools

ANALYSIS_SAMPLE_RATE = 44_100
ANALYSIS_CHANNELS = 2
SAMPLE_BYTES = 4
READ_BYTES = 1024 * 1024


class AnalysisCancelled(RuntimeError):
    """Raised when an analysis job is cancelled."""


@dataclass(frozen=True, slots=True)
class DecodedAudio:
    path: pathlib.Path
    sha256: str
    frames: int
    sample_rate: int
    channels: int
    duration_us: int


def decode_audio(
    source: pathlib.Path | str,
    destination: pathlib.Path | str,
    *,
    audio_track: int | None = None,
    start_us: int | None = None,
    end_us: int | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[float | None], None] | None = None,
) -> DecodedAudio:
    input_path = pathlib.Path(source).expanduser().resolve()
    output = pathlib.Path(destination).expanduser().resolve()
    if not input_path.is_file():
        raise MediaError("INPUT_NOT_FOUND", f"input media does not exist: {input_path}")
    if start_us is not None and start_us < 0:
        raise MediaError("INVALID_SEGMENT", "start_us must be non-negative")
    if end_us is not None and end_us <= (start_us or 0):
        raise MediaError("INVALID_SEGMENT", "end_us must be greater than start_us")

    try:
        tools = resolve_ffmpeg_tools()
        media = probe_media(input_path, tools=tools, cancelled=cancelled)
    except MediaError as exc:
        if exc.code == "FFMPEG_NOT_FOUND" and input_path.suffix.lower() in {
            ".wav",
            ".flac",
            ".ogg",
        }:
            return _decode_with_soundfile(
                input_path,
                output,
                start_us=start_us,
                end_us=end_us,
                cancelled=cancelled,
                progress=progress,
            )
        raise
    if not media.audio_streams:
        raise MediaError("NO_AUDIO_STREAM", "input has no audio stream")
    selected_stream: AudioStream | None
    if audio_track is None:
        selected_stream = next(
            (item for item in media.audio_streams if item.is_default),
            media.audio_streams[0],
        )
    else:
        selected_stream = next(
            (item for item in media.audio_streams if item.position == audio_track),
            None,
        )
        if selected_stream is None:
            raise MediaError("AUDIO_TRACK_NOT_FOUND", f"audio track {audio_track} does not exist")
    assert selected_stream is not None

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial")
    partial.unlink(missing_ok=True)
    command = [
        str(tools.ffmpeg),
        "-v",
        "error",
        "-nostdin",
        "-y",
    ]
    if start_us is not None:
        command.extend(["-ss", f"{start_us / 1_000_000:.6f}"])
    command.extend(["-i", str(input_path)])
    if end_us is not None:
        duration_us = end_us - (start_us or 0)
        command.extend(["-t", f"{duration_us / 1_000_000:.6f}"])
    command.extend(
        [
            "-map",
            f"0:{selected_stream.index}",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            str(ANALYSIS_CHANNELS),
            "-ar",
            str(ANALYSIS_SAMPLE_RATE),
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            "-",
        ]
    )
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    stderr_path = output.with_name(f".{output.name}.stderr.partial")
    stderr_path.unlink(missing_ok=True)
    stderr_stream = stderr_path.open("wb")
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=stderr_stream,
            creationflags=creation_flags,
        )
    except BaseException:
        stderr_stream.close()
        stderr_path.unlink(missing_ok=True)
        raise
    if process.stdout is None:
        process.kill()
        stderr_stream.close()
        stderr_path.unlink(missing_ok=True)
        raise MediaError("DECODE_FAILED", "FFmpeg stdout pipe is unavailable")
    estimated_bytes = (
        None
        if end_us is None
        else (end_us - (start_us or 0))
        * ANALYSIS_SAMPLE_RATE
        * ANALYSIS_CHANNELS
        * SAMPLE_BYTES
        // 1_000_000
    )
    hasher = hashlib.sha256()
    written = 0
    last_progress_fraction = -1.0
    last_progress_time = 0.0
    try:
        with partial.open("wb") as stream_output:
            while True:
                if cancelled is not None and cancelled():
                    process.kill()
                    raise AnalysisCancelled("analysis was cancelled")
                chunk = os.read(process.stdout.fileno(), READ_BYTES)
                if not chunk:
                    break
                stream_output.write(chunk)
                hasher.update(chunk)
                written += len(chunk)
                if progress is not None:
                    now = time.monotonic()
                    if estimated_bytes:
                        fraction = min(written / estimated_bytes, 1.0)
                        if fraction >= 1.0 or fraction - last_progress_fraction >= 0.01:
                            progress(fraction)
                            last_progress_fraction = fraction
                    elif now - last_progress_time >= 0.25:
                        progress(None)
                        last_progress_time = now
            stream_output.flush()
            os.fsync(stream_output.fileno())
        return_code = process.wait()
        stderr_stream.flush()
        if return_code != 0:
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            raise MediaError("DECODE_FAILED", stderr.strip() or f"FFmpeg exited with {return_code}")
        if written == 0 or written % (ANALYSIS_CHANNELS * SAMPLE_BYTES) != 0:
            raise MediaError("DECODE_EMPTY", "decoded audio is empty or misaligned")
        os.replace(partial, output)
    except BaseException:
        if process.poll() is None:
            process.kill()
        partial.unlink(missing_ok=True)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        stderr_stream.close()
        stderr_path.unlink(missing_ok=True)

    frames = written // (ANALYSIS_CHANNELS * SAMPLE_BYTES)
    return DecodedAudio(
        path=output,
        sha256=hasher.hexdigest(),
        frames=frames,
        sample_rate=ANALYSIS_SAMPLE_RATE,
        channels=ANALYSIS_CHANNELS,
        duration_us=round(frames * 1_000_000 / ANALYSIS_SAMPLE_RATE),
    )


def _decode_with_soundfile(
    input_path: pathlib.Path,
    output: pathlib.Path,
    *,
    start_us: int | None,
    end_us: int | None,
    cancelled: Callable[[], bool] | None,
    progress: Callable[[float | None], None] | None,
) -> DecodedAudio:
    try:
        samples, sample_rate = soundfile.read(
            input_path,
            dtype="float32",
            always_2d=True,
        )
    except (OSError, RuntimeError, soundfile.SoundFileRuntimeError) as exc:
        raise MediaError("DECODE_FAILED", str(exc)) from exc
    start_frame = 0 if start_us is None else round(start_us * sample_rate / 1_000_000)
    end_frame = len(samples) if end_us is None else round(end_us * sample_rate / 1_000_000)
    samples = samples[start_frame:end_frame]
    if samples.size == 0:
        raise MediaError("DECODE_EMPTY", "decoded audio is empty")
    if cancelled is not None and cancelled():
        raise AnalysisCancelled("analysis was cancelled")
    if sample_rate != ANALYSIS_SAMPLE_RATE:
        divisor = math_gcd(sample_rate, ANALYSIS_SAMPLE_RATE)
        samples = resample_poly(
            samples,
            ANALYSIS_SAMPLE_RATE // divisor,
            sample_rate // divisor,
            axis=0,
        ).astype(np.float32)
    mono = samples.mean(axis=1, dtype=np.float32)
    stereo = np.column_stack((mono, mono)).astype("<f4")
    payload = stereo.tobytes(order="C")
    partial = output.with_name(f".{output.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        with partial.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if progress is not None:
        progress(1.0)
    return DecodedAudio(
        path=output,
        sha256=hashlib.sha256(payload).hexdigest(),
        frames=len(stereo),
        sample_rate=ANALYSIS_SAMPLE_RATE,
        channels=ANALYSIS_CHANNELS,
        duration_us=round(len(stereo) * 1_000_000 / ANALYSIS_SAMPLE_RATE),
    )


def math_gcd(left: int, right: int) -> int:
    while right:
        left, right = right, left % right
    return abs(left)
