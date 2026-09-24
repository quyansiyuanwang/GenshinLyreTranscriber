"""Preview the effect of every global transpose candidate for a MIDI file."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from glt_core.domain.midi_import import MidiImportError, import_midi
from glt_core.processing.mapping import preview_transpositions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=pathlib.Path, help="Cleaned or source MIDI file")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()
    try:
        sequence = import_midi(args.input).sequence
        candidates = preview_transpositions(sequence)
    except (MidiImportError, ValueError) as exc:
        print(f"mapping preview failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {
                    "input": str(args.input.resolve()),
                    "selected_transpose": next(
                        item.transpose for item in candidates if item.selected
                    ),
                    "candidates": [
                        {
                            "transpose": item.transpose,
                            "score": list(item.score),
                            "selected": item.selected,
                            **item.stats.to_dict(),
                        }
                        for item in candidates
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0
    print("transpose  score  notes  replaced  folds  collisions  keys  selected")
    for item in candidates:
        print(
            f"{item.transpose:+3d}  {item.score[0]:5.1f}  "
            f"{item.stats.mapped_notes:5d}  {item.stats.replaced_semitones:8d}  "
            f"{item.stats.octave_folds:5d}  {item.stats.collision_notes_removed:10d}  "
            f"{item.stats.unique_keys_used:4d}  {'yes' if item.selected else ''}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
