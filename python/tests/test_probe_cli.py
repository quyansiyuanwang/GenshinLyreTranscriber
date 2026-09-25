from __future__ import annotations

import json
import pathlib

import pytest

from glt_core.media.ffmpeg import AudioStream, MediaInfo
from glt_core.probe_cli import main


def test_probe_cli_emits_track_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: pathlib.Path,
) -> None:
    source = tmp_path / "media.mkv"
    source.write_bytes(b"test")
    media = MediaInfo(
        path=source,
        format_name="matroska",
        duration_us=12_345_000,
        audio_streams=(
            AudioStream(0, 1, "flac", 48_000, 2, "jpn", "Main", True),
            AudioStream(1, 3, "aac", 44_100, 1, "eng", None, False),
        ),
    )
    monkeypatch.setattr("glt_core.probe_cli.probe_media", lambda _path: media)
    assert main(["--input", str(source)]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["duration_us"] == 12_345_000
    assert document["audio_streams"][0]["title"] == "Main"
    assert document["audio_streams"][1]["is_default"] is False
