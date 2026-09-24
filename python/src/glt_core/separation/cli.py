"""CLI adapter for a future packaged Demucs component."""

from __future__ import annotations

import argparse
import json
import pathlib
from collections.abc import Sequence

from glt_core.separation.component import ComponentVerificationError
from glt_core.separation.provider import SeparationError, run_component


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glt-worker separate")
    parser.add_argument("--component", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    try:
        run = run_component(
            pathlib.Path(args.component),
            pathlib.Path(args.input),
            pathlib.Path(args.output),
            model_id=args.model,
            progress=lambda stage, fraction: _write(
                {"type": "progress", "stage": stage, "fraction": fraction}
            ),
        )
    except (ComponentVerificationError, SeparationError, OSError) as exc:
        _write({"type": "error", "message": str(exc)})
        return 2
    _write(
        {
            "type": "result",
            "stem_set_path": str(run.output_directory / "stem-set-v1.json"),
            "elapsed_seconds": run.elapsed_seconds,
        }
    )
    return 0


def _write(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
