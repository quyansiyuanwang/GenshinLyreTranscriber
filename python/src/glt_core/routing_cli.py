"""CLI adapter for deterministic stem routing previews."""

from __future__ import annotations

import argparse
import json
import pathlib
from collections.abc import Sequence

from glt_core.processing.routing import (
    load_routing_plan,
    route_stem_audio,
    routing_plan_from_template,
    write_routing_plan,
)
from glt_core.separation.stem_set import validate_stem_set


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glt-worker route")
    parser.add_argument("--stem-set", required=True)
    parser.add_argument("--output", required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--mode",
        choices=("solo", "melody_chords", "two_voice", "full", "custom"),
    )
    source.add_argument("--plan-json")
    parser.add_argument("--max-voices", type=int)
    args = parser.parse_args(argv)
    stem_set_path = pathlib.Path(args.stem_set).expanduser().resolve()
    output = pathlib.Path(args.output).expanduser().resolve()
    try:
        document = json.loads(stem_set_path.read_text(encoding="utf-8"))
        stem_set = validate_stem_set(document, stem_set_path.parent)
        if args.plan_json:
            plan_document = json.loads(args.plan_json)
            if not isinstance(plan_document, dict):
                raise ValueError("routing plan JSON must decode to an object")
            plan = load_routing_plan(plan_document)
        else:
            plan = routing_plan_from_template(args.mode or "solo", max_voices=args.max_voices)
        output.mkdir(parents=True, exist_ok=True)
        stem_paths = {
            stem.role: stem_set_path.parent / stem.relative_path for stem in stem_set.stems
        }
        routed_audio = route_stem_audio(
            stem_paths,
            plan,
            output / "routed-mix.wav",
        )
        routing_plan_path = write_routing_plan(plan, output / "routing-plan-v1.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _write({"type": "error", "message": str(exc)})
        return 2
    _write(
        {
            "type": "result",
            "routed_audio": str(routed_audio),
            "routing_plan_path": str(routing_plan_path),
            "mode": plan.mode,
            "active_routes": len(plan.active_routes()),
        }
    )
    return 0


def _write(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
