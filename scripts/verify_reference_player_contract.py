"""Verify a generated compatibility score with the pinned GIPianoPlayer checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
from typing import Any

REFERENCE_URL = "https://github.com/quyansiyuanwang/GenshinImpactPianoPlayer.git"
REFERENCE_COMMIT = "7aaddad21b71e91fd36b6ce7687dfb546076f602"
TICK_US = 10_000

EVENTS: list[dict[str, Any]] = [
    {"at_us": 5_000, "keys": ["A"]},
    {"at_us": 25_000, "keys": ["C", "E"]},
    {"at_us": 100_000, "keys": ["G"]},
    {"at_us": 110_000, "keys": ["S"]},
    {"at_us": 1_000_000, "keys": ["A", "D", "G"]},
]


class VerificationError(RuntimeError):
    pass


def _git_output(root: pathlib.Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_checkout(root: pathlib.Path) -> dict[str, Any]:
    if not root.is_dir():
        raise VerificationError(f"reference player root does not exist: {root}")
    remote = _git_output(root, "remote", "get-url", "origin")
    commit = _git_output(root, "rev-parse", "HEAD")
    status = _git_output(root, "status", "--porcelain=v1")
    if remote != REFERENCE_URL:
        raise VerificationError(f"unexpected reference remote: {remote}")
    if commit != REFERENCE_COMMIT:
        raise VerificationError(f"expected commit {REFERENCE_COMMIT}, got {commit}")
    if status:
        raise VerificationError("reference player worktree is not clean")
    parser_path = root / "src/core/parser/score_parser.py"
    player_path = root / "src/core/player/player.py"
    return {
        "remote": remote,
        "commit": commit,
        "clean": True,
        "parser_sha256": _sha256(parser_path),
        "player_sha256": _sha256(player_path),
    }


def _load_reference(root: pathlib.Path) -> tuple[Any, Any]:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from src.core.parser.score_parser import ScoreParser
    from src.core.player.player import Player

    return ScoreParser, Player


class VirtualKeyboard:
    def __init__(self, player: Any) -> None:
        self.player = player
        self.operations: list[dict[str, Any]] = []

    def _record(self, keys: list[str]) -> None:
        self.operations.append(
            {
                "at_us": int(self.player.virtual_time_us),
                "keys": sorted(keys),
            }
        )

    def press_key(self, key: str) -> None:
        self._record([key])

    def release_key(self, _key: str) -> None:
        pass

    def tap_key(self, key: str) -> None:
        self._record([key])

    def press_keys_simultaneously(self, keys: list[str]) -> None:
        self._record(keys)


def _virtual_player_type(reference_player: Any) -> Any:
    class VirtualPlayer(reference_player):
        def __init__(self, score: Any, keyboard: VirtualKeyboard) -> None:
            self.virtual_time_us = 0
            super().__init__(score, keyboard)

        def _wait(self, duration: float, _generation: int) -> bool:
            self.virtual_time_us += round(max(0.0, duration) * 1_000_000)
            return True

    return VirtualPlayer


def _expected_schedule(document: dict[str, Any]) -> dict[int, tuple[str, ...]]:
    grouped: dict[int, set[str]] = {}
    for event in document["events"]:
        slot = (int(event["at_us"]) + TICK_US // 2) // TICK_US
        grouped.setdefault(slot, set()).update(str(key) for key in event["keys"])
    return {slot: tuple(sorted(keys)) for slot, keys in sorted(grouped.items())}


def run(
    player_root: pathlib.Path,
    *,
    compat_score: pathlib.Path | None = None,
    events_json: pathlib.Path | None = None,
) -> dict[str, Any]:
    if (compat_score is None) != (events_json is None):
        raise VerificationError(
            "--compat-score and --events-json must be provided together"
        )

    project_root = pathlib.Path(__file__).resolve().parents[1]
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(project_root / "python/src"))

    checkout = _verify_checkout(player_root)
    ScoreParser, reference_player = _load_reference(player_root)
    if compat_score is None or events_json is None:
        from glt_core.export import build_compatibility_score

        document = {
            "format_version": 1,
            "time_unit": "us",
            "duration_us": 2_000_000,
            "events": EVENTS,
            "metadata": {"generator": "contract", "mapping_profile": "lyre-21-default"},
        }
        compatibility = build_compatibility_score(document)
        compatibility_text = compatibility.text
        source = "builtin_fixture"
    else:
        document = json.loads(events_json.read_text(encoding="utf-8"))
        compatibility_text = compat_score.read_text(encoding="utf-8")
        source = "external_artifacts"

    with tempfile.TemporaryDirectory(prefix="glt-reference-contract-") as directory:
        score_path = pathlib.Path(directory) / "score.compat.txt"
        score_path.write_text(compatibility_text, encoding="utf-8", newline="")
        parsed = ScoreParser(str(score_path)).parse()

    if parsed.warnings:
        raise VerificationError(f"reference parser warnings: {parsed.warnings}")
    config = parsed.config
    expected_config = {
        "interval_rating": 0.01,
        "line_interval_rating": 0.0,
        "space_interval_rating": 1.0,
        "empty_line_interval_rating": 0.0,
        "segment_length": 0,
        "segment_strict": False,
        "speed_multiplier": 1.0,
        "loop": False,
    }
    for name, expected in expected_config.items():
        actual = getattr(config, name)
        if actual != expected:
            raise VerificationError(f"{name}: expected {expected!r}, got {actual!r}")

    player_type = _virtual_player_type(reference_player)

    # Construct through the reference Player class while giving the keyboard access
    # to the virtual clock populated by VirtualPlayer._wait.
    keyboard = VirtualKeyboard(player=None)
    player = player_type(parsed, keyboard)
    keyboard.player = player
    player._playback_loop()

    actual: dict[int, tuple[str, ...]] = {}
    for operation in keyboard.operations:
        if operation["at_us"] % TICK_US:
            raise VerificationError(f"operation is off the 10ms grid: {operation}")
        slot = operation["at_us"] // TICK_US
        if slot in actual:
            raise VerificationError(f"multiple operations landed in slot {slot}")
        actual[slot] = tuple(operation["keys"])
    expected = _expected_schedule(document)
    if actual != expected:
        raise VerificationError(f"schedule mismatch: expected {expected}, got {actual}")

    events = document["events"]
    max_onset_error_us = max(
        (
            abs(
                ((int(event["at_us"]) + TICK_US // 2) // TICK_US) * TICK_US
                - int(event["at_us"])
            )
            for event in events
        ),
        default=0,
    )
    if max_onset_error_us > TICK_US // 2:
        raise VerificationError(
            f"onset rounding error exceeds 5ms: {max_onset_error_us}us"
        )
    return {
        "source": source,
        "reference": checkout,
        "warnings": parsed.warnings,
        "expected_schedule": {str(slot): list(keys) for slot, keys in expected.items()},
        "actual_schedule": {str(slot): list(keys) for slot, keys in actual.items()},
        "compatibility_sha256": hashlib.sha256(
            compatibility_text.encode("utf-8")
        ).hexdigest(),
        "max_onset_error_us": max_onset_error_us,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--player-root", required=True, type=pathlib.Path)
    parser.add_argument("--compat-score", type=pathlib.Path)
    parser.add_argument("--events-json", type=pathlib.Path)
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    try:
        result = run(
            args.player_root.resolve(),
            compat_score=args.compat_score.resolve() if args.compat_score else None,
            events_json=args.events_json.resolve() if args.events_json else None,
        )
    except (OSError, subprocess.CalledProcessError, VerificationError) as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8", newline="")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
