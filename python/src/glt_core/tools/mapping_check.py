"""Generate a reproducible 21-key pitch/order listening check."""

from __future__ import annotations

import argparse
import json
import pathlib

import numpy as np
import soundfile

from glt_core.processing.mapping import default_mapping_layout

SAMPLE_RATE = 44_100
NOTE_SECONDS = 0.45
GAP_SECONDS = 0.16


def generate_mapping_check(
    output_directory: pathlib.Path | str,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Write a WAV with one tone per key plus its machine-readable manifest."""
    output = pathlib.Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    layout = default_mapping_layout()
    samples: list[np.ndarray] = []
    for entry in layout.keys:
        note_frames = round(NOTE_SECONDS * SAMPLE_RATE)
        time = np.arange(note_frames, dtype=np.float64) / SAMPLE_RATE
        frequency = 440.0 * (2.0 ** ((entry.pitch - 69) / 12.0))
        envelope = np.minimum.reduce(
            [
                np.minimum(1.0, np.arange(note_frames) / (0.02 * SAMPLE_RATE)),
                np.minimum(1.0, (note_frames - np.arange(note_frames)) / (0.05 * SAMPLE_RATE)),
            ]
        )
        tone = 0.28 * envelope * np.sin(2.0 * np.pi * frequency * time)
        samples.append(tone.astype(np.float32))
        samples.append(np.zeros(round(GAP_SECONDS * SAMPLE_RATE), dtype=np.float32))
    wav_path = output / "mapping-check.wav"
    manifest_path = output / "mapping-check.json"
    soundfile.write(wav_path, np.concatenate(samples), SAMPLE_RATE, subtype="PCM_16")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mapping_profile": layout.profile,
                "sample_rate": SAMPLE_RATE,
                "note_seconds": NOTE_SECONDS,
                "gap_seconds": GAP_SECONDS,
                "entries": [{"key": entry.key, "midi_pitch": entry.pitch} for entry in layout.keys],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return wav_path, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=pathlib.Path, help="Destination directory")
    args = parser.parse_args()
    wav_path, manifest_path = generate_mapping_check(args.output)
    print(wav_path)
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
