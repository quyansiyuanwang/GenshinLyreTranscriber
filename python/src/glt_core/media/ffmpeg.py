"""Structured ffprobe parsing and safe FFmpeg audio extraction."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

MODEL_SAMPLE_RATE = 22_050
MODEL_CHANNELS = 1
SAFE_INTEGER_MAX = 9_007_199_254_740_991
MAX_DIAGNOSTIC_BYTES = 64 * 1024


class MediaError(RuntimeError):
    """A stable media failure suitable for a worker error message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FfmpegTools:
    ffmpeg: pathlib.Path
    ffprobe: pathlib.Path


@dataclass(frozen=True, slots=True)
class AudioStream:
    position: int
    index: int
    codec_name: str
    sample_rate: int | None
    channels: int | None
    language: str | None
    title: str | None
    is_default: bool


@dataclass(frozen=True, slots=True)
class MediaInfo:
    path: pathlib.Path
    format_name: str
    duration_us: int
    audio_streams: tuple[AudioStream, ...]


@dataclass(frozen=True, slots=True)
class AudioExtractionPlan:
    input_path: pathlib.Path
    output_path: pathlib.Path
    stream: AudioStream
    start_us: int
    end_us: int
    sample_rate: int = MODEL_SAMPLE_RATE
    channels: int = MODEL_CHANNELS


def resolve_ffmpeg_tools(
    *,
    ffmpeg: pathlib.Path | str | None = None,
    ffprobe: pathlib.Path | str | None = None,
) -> FfmpegTools:
    """Resolve explicit tools, packaged resources, or development PATH entries."""
    if ffmpeg is not None or ffprobe is not None:
        if ffmpeg is None or ffprobe is None:
            raise MediaError("FFMPEG_NOT_FOUND", "both ffmpeg and ffprobe paths are required")
        return _validate_tools(pathlib.Path(ffmpeg), pathlib.Path(ffprobe))

    directory = os.environ.get("GLT_FFMPEG_DIR")
    if directory:
        root = pathlib.Path(directory)
        return _validate_tools(root / _tool_name("ffmpeg"), root / _tool_name("ffprobe"))

    configured_ffmpeg = os.environ.get("GLT_FFMPEG")
    configured_ffprobe = os.environ.get("GLT_FFPROBE")
    if configured_ffmpeg or configured_ffprobe:
        if not configured_ffmpeg or not configured_ffprobe:
            raise MediaError(
                "FFMPEG_NOT_FOUND",
                "GLT_FFMPEG and GLT_FFPROBE must be set together",
            )
        return _validate_tools(pathlib.Path(configured_ffmpeg), pathlib.Path(configured_ffprobe))

    if getattr(sys, "frozen", False):
        frozen_root = pathlib.Path(getattr(sys, "_MEIPASS", "")) / "ffmpeg/bin"
        return _validate_tools(
            frozen_root / _tool_name("ffmpeg"),
            frozen_root / _tool_name("ffprobe"),
        )

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path is None or ffprobe_path is None:
        raise MediaError("FFMPEG_NOT_FOUND", "ffmpeg and ffprobe are not available")
    return _validate_tools(pathlib.Path(ffmpeg_path), pathlib.Path(ffprobe_path))


def _tool_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _validate_tools(ffmpeg: pathlib.Path, ffprobe: pathlib.Path) -> FfmpegTools:
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise MediaError(
            "FFMPEG_NOT_FOUND",
            f"FFmpeg tools are missing: {ffmpeg} / {ffprobe}",
        )
    return FfmpegTools(ffmpeg=ffmpeg.resolve(), ffprobe=ffprobe.resolve())


def probe_media(
    input_path: pathlib.Path | str,
    *,
    tools: FfmpegTools | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> MediaInfo:
    """Run ffprobe and parse its JSON output."""
    path = pathlib.Path(input_path).expanduser().resolve()
    if not path.is_file():
        raise MediaError("INPUT_NOT_FOUND", f"input media does not exist: {path}")
    tools = tools or resolve_ffmpeg_tools()
    command = [
        str(tools.ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    completed = _run_process(command, cancelled=cancelled)
    if completed.returncode != 0:
        raise MediaError("PROBE_FAILED", _error_message(completed.stderr))
    try:
        payload = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaError("PROBE_INVALID", "ffprobe did not return valid JSON") from exc
    return parse_ffprobe_payload(path, payload)


def parse_ffprobe_payload(path: pathlib.Path, payload: Any) -> MediaInfo:
    if not isinstance(payload, dict):
        raise MediaError("PROBE_INVALID", "ffprobe payload must be an object")
    format_data = payload.get("format")
    streams_data = payload.get("streams")
    if not isinstance(format_data, dict) or not isinstance(streams_data, list):
        raise MediaError("PROBE_INVALID", "ffprobe payload is missing format or streams")
    duration_us = _duration_us(format_data.get("duration"))
    audio_streams: list[AudioStream] = []
    for stream in streams_data:
        if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
            continue
        index = stream.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise MediaError("PROBE_INVALID", "audio stream index is invalid")
        disposition = stream.get("disposition")
        is_default = isinstance(disposition, dict) and disposition.get("default") == 1
        tags_value = stream.get("tags")
        tags: dict[Any, Any] = tags_value if isinstance(tags_value, dict) else {}
        audio_streams.append(
            AudioStream(
                position=len(audio_streams),
                index=index,
                codec_name=str(stream.get("codec_name") or "unknown"),
                sample_rate=_optional_positive_int(stream.get("sample_rate")),
                channels=_optional_positive_int(stream.get("channels")),
                language=_optional_text(tags.get("language")),
                title=_optional_text(tags.get("title")),
                is_default=is_default,
            )
        )
    if not audio_streams:
        raise MediaError("NO_AUDIO_STREAM", "input has no audio stream")
    return MediaInfo(
        path=path,
        format_name=str(format_data.get("format_name") or "unknown"),
        duration_us=duration_us,
        audio_streams=tuple(audio_streams),
    )


def select_audio_stream(media: MediaInfo, audio_track: int | None = None) -> AudioStream:
    """Select a zero-based audio-stream position, or the default/first stream."""
    if audio_track is not None:
        if isinstance(audio_track, bool) or audio_track < 0:
            raise MediaError("INVALID_AUDIO_TRACK", "audio_track must be a non-negative integer")
        try:
            return media.audio_streams[audio_track]
        except IndexError as exc:
            raise MediaError(
                "INVALID_AUDIO_TRACK",
                f"audio track {audio_track} does not exist",
            ) from exc
    return next(
        (stream for stream in media.audio_streams if stream.is_default),
        media.audio_streams[0],
    )


def build_extraction_plan(
    media: MediaInfo,
    output_path: pathlib.Path | str,
    *,
    audio_track: int | None = None,
    start_us: int | None = None,
    end_us: int | None = None,
) -> AudioExtractionPlan:
    stream = select_audio_stream(media, audio_track)
    if media.duration_us <= 0:
        raise MediaError("DURATION_UNKNOWN", "input duration is missing or zero")
    start = _validate_time(0 if start_us is None else start_us, "start_us")
    end = _validate_time(media.duration_us if end_us is None else end_us, "end_us")
    if end <= start:
        raise MediaError("INVALID_RANGE", "end_us must be greater than start_us")
    if end > media.duration_us:
        raise MediaError("INVALID_RANGE", "end_us exceeds media duration")
    return AudioExtractionPlan(
        input_path=media.path,
        output_path=pathlib.Path(output_path).expanduser().resolve(),
        stream=stream,
        start_us=start,
        end_us=end,
    )


def extract_audio(
    plan: AudioExtractionPlan,
    *,
    tools: FfmpegTools | None = None,
    overwrite: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> pathlib.Path:
    """Decode one selected track atomically to the model WAV format."""
    tools = tools or resolve_ffmpeg_tools()
    if plan.output_path.exists() and not overwrite:
        raise MediaError("OUTPUT_EXISTS", f"output already exists: {plan.output_path}")
    try:
        plan.output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MediaError("OUTPUT_FAILED", f"cannot create output directory: {exc}") from exc
    partial = plan.output_path.with_name(f".{plan.output_path.name}.partial")
    partial.unlink(missing_ok=True)
    command = build_ffmpeg_command(tools.ffmpeg, plan, partial)
    try:
        completed = _run_process(command, cancelled=cancelled)
        if completed.returncode != 0:
            raise MediaError("EXTRACT_FAILED", _error_message(completed.stderr))
        if not partial.is_file() or partial.stat().st_size == 0:
            raise MediaError("EXTRACT_FAILED", "FFmpeg did not produce an audio stream")
        os.replace(partial, plan.output_path)
    except MediaError:
        partial.unlink(missing_ok=True)
        raise
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise MediaError("OUTPUT_FAILED", f"cannot commit extracted audio: {exc}") from exc
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return plan.output_path


def build_ffmpeg_command(
    ffmpeg: pathlib.Path,
    plan: AudioExtractionPlan,
    output_path: pathlib.Path,
) -> list[str]:
    duration_seconds = (plan.end_us - plan.start_us) / 1_000_000
    return [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(plan.input_path),
        "-map",
        f"0:{plan.stream.index}",
        "-map_metadata",
        "-1",
        "-ss",
        f"{plan.start_us / 1_000_000:.6f}",
        "-t",
        f"{duration_seconds:.6f}",
        "-vn",
        "-ac",
        str(plan.channels),
        "-ar",
        str(plan.sample_rate),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(output_path),
    ]


def _run_process(
    command: Sequence[str],
    *,
    cancelled: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags,
    )
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if cancelled is not None and cancelled():
                _terminate_process(process)
                process.communicate()
                raise MediaError("CANCELLED", "media operation was cancelled") from None
    return subprocess.CompletedProcess(
        args=list(command),
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def sha256_file(path: pathlib.Path | str) -> str:
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _duration_us(value: Any) -> int:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise MediaError("DURATION_UNKNOWN", "media duration is missing") from exc
    if not 0 <= seconds <= SAFE_INTEGER_MAX / 1_000_000:
        raise MediaError("DURATION_UNKNOWN", "media duration is invalid")
    return round(seconds * 1_000_000)


def _validate_time(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= SAFE_INTEGER_MAX:
        raise MediaError("INVALID_RANGE", f"{name} must be a safe non-negative integer")
    return value


def _optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_text(value: Any) -> str | None:
    return str(value) if value else None


def _error_message(stderr: bytes) -> str:
    if not stderr:
        return "FFmpeg returned a non-zero exit status"
    return stderr[-MAX_DIAGNOSTIC_BYTES:].decode("utf-8", errors="replace").strip()
