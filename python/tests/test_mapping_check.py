from __future__ import annotations

import json
import pathlib

import soundfile

from glt_core.processing.mapping import default_mapping_layout
from glt_core.tools.mapping_check import generate_mapping_check


def test_mapping_check_wav_and_manifest(tmp_path: pathlib.Path) -> None:
    wav_path, manifest_path = generate_mapping_check(tmp_path)
    info = soundfile.info(wav_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    layout = default_mapping_layout()
    assert info.samplerate == 44_100
    assert info.channels == 1
    assert [entry["key"] for entry in manifest["entries"]] == [entry.key for entry in layout.keys]
    assert [entry["midi_pitch"] for entry in manifest["entries"]] == [
        entry.pitch for entry in layout.keys
    ]
