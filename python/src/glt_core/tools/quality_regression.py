"""Synthetic quality regression fixture with reproducible event F1."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import soundfile as sf

from glt_core.processing.mapping import default_mapping_layout


@dataclass(frozen=True, slots=True)
class ExpectedEvent:
    at_us: int
    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatchScore:
    true_positive: int
    false_positive: int
    false_negative: int
    precision: float
    recall: float
    f1: float


def match_events(
    expected: Sequence[ExpectedEvent],
    actual: Sequence[ExpectedEvent],
    tolerance_us: int,
) -> MatchScore:
    used = set()
    true_positive = 0
    for wanted in expected:
        candidates = [
            (index, candidate)
            for index, candidate in enumerate(actual)
            if index not in used
            and abs(candidate.at_us - wanted.at_us) <= tolerance_us
            and set(candidate.keys) == set(wanted.keys)
        ]
        if not candidates:
            continue
        index, _ = min(candidates, key=lambda item: abs(item[1].at_us - wanted.at_us))
        used.add(index)
        true_positive += 1
    false_negative = len(expected) - true_positive
    false_positive = len(actual) - true_positive
    precision = true_positive / max(1, true_positive + false_positive)
    recall = true_positive / max(1, true_positive + false_negative)
    f1 = 2 * precision * recall / max(1e-12, precision + recall)
    return MatchScore(true_positive, false_positive, false_negative, precision, recall, f1)


def expected_fixture() -> list[ExpectedEvent]:
    layout = default_mapping_layout()
    key_by_pitch = {entry.pitch: entry.key for entry in layout.keys}
    pitches = [60, 64, 67, 72, 76, 79]
    return [
        ExpectedEvent(at_us=(index + 1) * 500_000, keys=(key_by_pitch[pitch],))
        for index, pitch in enumerate(pitches)
    ]


def write_fixture(path: pathlib.Path, sample_rate: int = 22050) -> None:
    duration_seconds = 4.0
    samples = np.zeros(int(sample_rate * duration_seconds), dtype=np.float32)
    pitches = [midi_to_hz(pitch) for pitch in [60, 64, 67, 72, 76, 79]]
    for index, frequency in enumerate(pitches):
        start = int((index + 1) * 0.5 * sample_rate)
        length = int(0.42 * sample_rate)
        time = np.arange(length, dtype=np.float32) / sample_rate
        envelope = np.minimum(1.0, np.minimum(time / 0.02, (0.42 - time) / 0.05))
        samples[start : start + length] += 0.48 * np.sin(2 * np.pi * frequency * time) * envelope
    sf.write(path, samples, sample_rate, subtype="PCM_16")


def midi_to_hz(pitch: int) -> float:
    return float(440.0 * 2.0 ** ((pitch - 69) / 12.0))


def read_events(path: pathlib.Path) -> list[ExpectedEvent]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return [
        ExpectedEvent(at_us=int(event["at_us"]), keys=tuple(sorted(event["keys"])))
        for event in document["events"]
    ]


def run_quality_regression(
    glt: pathlib.Path,
    worker: pathlib.Path | None,
    minimum_f1: float,
    tolerance_us: int,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="glt-quality-") as temporary:
        root = pathlib.Path(temporary)
        source = root / "synthetic-steps.wav"
        output = root / "result"
        write_fixture(source)
        command = [
            str(glt),
            "transcribe",
            str(source),
            "--output",
            str(output),
            "--timing",
            "preserve",
            "--transpose",
            "0",
            "--arrangement",
            "off",
            "--min-confidence",
            "0.1",
            "--min-duration-ms",
            "50",
            "--json",
        ]
        if worker is not None:
            command.extend(["--worker", str(worker)])
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        expected = expected_fixture()
        actual = read_events(output / "score.events.json")
        score = match_events(expected, actual, tolerance_us)
        result: dict[str, object] = {
            "minimum_f1": minimum_f1,
            "tolerance_us": tolerance_us,
            "expected_events": len(expected),
            "actual_events": len(actual),
            "true_positive": score.true_positive,
            "false_positive": score.false_positive,
            "false_negative": score.false_negative,
            "precision": round(score.precision, 6),
            "recall": round(score.recall, 6),
            "f1": round(score.f1, 6),
        }
        if score.f1 + 1e-12 < minimum_f1:
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--glt", type=pathlib.Path, required=True)
    parser.add_argument("--worker", type=pathlib.Path)
    parser.add_argument("--minimum-f1", type=float, default=0.98)
    parser.add_argument("--tolerance-us", type=int, default=120_000)
    args = parser.parse_args(argv)
    result = run_quality_regression(
        args.glt,
        args.worker,
        args.minimum_f1,
        args.tolerance_us,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
