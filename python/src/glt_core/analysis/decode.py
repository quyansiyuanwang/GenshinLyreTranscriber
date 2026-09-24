"""Streaming decode to the canonical analysis PCM format."""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

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

    media = probe_media(input_path, cancelled=cancelled)
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
    tools = resolve_ffmpeg_tools()
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
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags,
    )
    if process.stdout is None:
        process.kill()
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
    try:
        with partial.open("wb") as stream_output:
            while True:
                if cancelled is not None and cancelled():
                    process.kill()
                    raise AnalysisCancelled("analysis was cancelled")
                chunk = process.stdout.read(READ_BYTES)
                if not chunk:
                    break
                stream_output.write(chunk)
                hasher.update(chunk)
                written += len(chunk)
                if progress is not None:
                    progress(None if not estimated_bytes else min(written / estimated_bytes, 1.0))
            stream_output.flush()
            os.fsync(stream_output.fileno())
        return_code = process.wait()
        if return_code != 0:
            stderr = (
                process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            )
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
        if process.stderr is not None:
            process.stderr.close()

    frames = written // (ANALYSIS_CHANNELS * SAMPLE_BYTES)
    return DecodedAudio(
        path=output,
        sha256=hasher.hexdigest(),
        frames=frames,
        sample_rate=ANALYSIS_SAMPLE_RATE,
        channels=ANALYSIS_CHANNELS,
        duration_us=round(frames * 1_000_000 / ANALYSIS_SAMPLE_RATE),
    )
