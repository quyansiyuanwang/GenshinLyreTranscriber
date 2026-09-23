from __future__ import annotations

import pathlib
import subprocess
from typing import Any

import pytest

from glt_core.media import (
    AudioExtractionPlan,
    FfmpegTools,
    JobWorkspace,
    MediaError,
    build_extraction_plan,
    build_ffmpeg_command,
    extract_audio,
    parse_ffprobe_payload,
    select_audio_stream,
)
from glt_core.media import ffmpeg as ffmpeg_module


def _probe_payload() -> dict[str, Any]:
    return {
        "format": {"format_name": "mov,mp4", "duration": "2.500000"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "44100",
                "channels": 2,
                "disposition": {"default": 0},
                "tags": {"language": "jpn", "title": "first"},
            },
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 1,
                "disposition": {"default": 1},
                "tags": {"language": "eng"},
            },
        ],
    }


def test_parse_and_select_default_audio_stream(tmp_path: pathlib.Path) -> None:
    media = parse_ffprobe_payload(tmp_path / "input.mp4", _probe_payload())
    assert media.duration_us == 2_500_000
    assert [stream.position for stream in media.audio_streams] == [0, 1]
    assert [stream.index for stream in media.audio_streams] == [1, 2]
    assert select_audio_stream(media).position == 1
    assert select_audio_stream(media, 0).language == "jpn"


def test_reject_missing_audio_stream(tmp_path: pathlib.Path) -> None:
    payload = _probe_payload()
    payload["streams"] = [{"index": 0, "codec_type": "video"}]
    with pytest.raises(MediaError, match="no audio"):
        parse_ffprobe_payload(tmp_path / "video.mp4", payload)


def test_build_extraction_plan_validates_range(tmp_path: pathlib.Path) -> None:
    media = parse_ffprobe_payload(tmp_path / "input.mp4", _probe_payload())
    plan = build_extraction_plan(
        media,
        tmp_path / "output.wav",
        audio_track=0,
        start_us=250_000,
        end_us=1_250_000,
    )
    assert plan.stream.index == 1
    assert plan.start_us == 250_000
    assert plan.end_us == 1_250_000
    with pytest.raises(MediaError, match="greater"):
        build_extraction_plan(media, tmp_path / "bad.wav", start_us=2, end_us=1)
    with pytest.raises(MediaError, match="duration"):
        build_extraction_plan(media, tmp_path / "bad.wav", end_us=3_000_000)

    unknown_duration = dict(_probe_payload())
    unknown_duration["format"] = {"format_name": "wav", "duration": "0"}
    zero_media = parse_ffprobe_payload(tmp_path / "zero.wav", unknown_duration)
    with pytest.raises(MediaError, match="duration"):
        build_extraction_plan(zero_media, tmp_path / "zero-output.wav")


def test_ffmpeg_command_uses_argument_array(tmp_path: pathlib.Path) -> None:
    plan = AudioExtractionPlan(
        input_path=tmp_path / "space & quote'.mp4",
        output_path=tmp_path / "partial.wav",
        stream=select_audio_stream(parse_ffprobe_payload(tmp_path / "x.mp4", _probe_payload()), 0),
        start_us=250_000,
        end_us=1_250_000,
    )
    command = build_ffmpeg_command(pathlib.Path("ffmpeg"), plan, plan.output_path)
    assert command[0] == "ffmpeg"
    assert command[command.index("-map") + 1] == "0:1"
    assert command[command.index("-ss") + 1] == "0.250000"
    assert command[command.index("-t") + 1] == "1.000000"
    assert command[command.index("-ar") + 1] == "22050"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-map_metadata") + 1] == "-1"
    assert command[-3:] == ["-f", "wav", str(plan.output_path)]


def test_extract_audio_is_atomic(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = AudioExtractionPlan(
        input_path=tmp_path / "input.wav",
        output_path=tmp_path / "output.wav",
        stream=select_audio_stream(parse_ffprobe_payload(tmp_path / "x.mp4", _probe_payload())),
        start_us=0,
        end_us=1_000_000,
    )

    def successful(
        command: list[str], *, cancelled: object = None
    ) -> subprocess.CompletedProcess[bytes]:
        pathlib.Path(command[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(ffmpeg_module, "_run_process", successful)
    output = extract_audio(
        plan,
        tools=FfmpegTools(pathlib.Path("ffmpeg"), pathlib.Path("ffprobe")),
    )
    assert output.read_bytes() == b"RIFF"
    assert not output.with_name(f".{output.name}.partial").exists()

    def failed(
        command: list[str], *, cancelled: object = None
    ) -> subprocess.CompletedProcess[bytes]:
        pathlib.Path(command[-1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(command, 1, b"", b"failed")

    monkeypatch.setattr(ffmpeg_module, "_run_process", failed)
    failed_plan = AudioExtractionPlan(
        input_path=plan.input_path,
        output_path=tmp_path / "failed.wav",
        stream=plan.stream,
        start_us=0,
        end_us=1_000_000,
    )
    with pytest.raises(MediaError, match="failed"):
        extract_audio(
            failed_plan,
            tools=FfmpegTools(pathlib.Path("ffmpeg"), pathlib.Path("ffprobe")),
        )
    assert not failed_plan.output_path.exists()
    partial = failed_plan.output_path.with_name(f".{failed_plan.output_path.name}.partial")
    assert not partial.exists()

    blocking_parent = tmp_path / "not-a-directory"
    blocking_parent.write_bytes(b"file")
    with pytest.raises(MediaError, match="output directory"):
        extract_audio(
            AudioExtractionPlan(
                input_path=plan.input_path,
                output_path=blocking_parent / "output.wav",
                stream=plan.stream,
                start_us=0,
                end_us=1_000_000,
            ),
            tools=FfmpegTools(pathlib.Path("ffmpeg"), pathlib.Path("ffprobe")),
        )


def test_job_workspace_cleans_only_inside_root(tmp_path: pathlib.Path) -> None:
    with JobWorkspace.create(tmp_path, "job-001") as workspace:
        marker = workspace.path / "extract.wav"
        marker.write_bytes(b"data")
        assert marker.exists()
    assert not workspace.path.exists()

    with pytest.raises(MediaError, match="unsupported"):
        JobWorkspace.create(tmp_path, "../escape")
