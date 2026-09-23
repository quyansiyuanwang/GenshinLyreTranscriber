"""CLI entry point for the minimal frozen ONNX worker probe."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from glt_core.transcription.onnx_probe import transcribe_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one Basic Pitch ONNX transcription.")
    parser.add_argument("input", type=pathlib.Path, help="Local WAV input path")
    parser.add_argument("output", type=pathlib.Path, help="Destination MIDI path")
    parser.add_argument("--model", type=pathlib.Path, help="Override the bundled ONNX model")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = transcribe_file(args.input, args.output, model_path=args.model)
    except Exception as exc:
        print(f"transcription failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.to_json_dict(), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
