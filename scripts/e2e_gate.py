"""Repeatable workstation E2E and release-gate report."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import soundfile


@dataclass(frozen=True, slots=True)
class ProcessResult:
    command: list[str]
    elapsed_seconds: float
    peak_working_set_bytes: int
    returncode: int
    stdout: str
    stderr: str


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _peak_working_set_bytes(pid: int) -> int:
    if os.name != "nt":
        return 0
    process_query_limited_information = 0x1000
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return 0
    try:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), ctypes.sizeof(counters)
        ):
            return 0
        return int(counters.PeakWorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


def run_monitored(
    command: list[str], *, cwd: pathlib.Path | None = None
) -> ProcessResult:
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
    )
    peak = 0
    while process.poll() is None:
        peak = max(peak, _peak_working_set_bytes(process.pid))
        time.sleep(0.05)
    peak = max(peak, _peak_working_set_bytes(process.pid))
    stdout, stderr = process.communicate()
    return ProcessResult(
        command=command,
        elapsed_seconds=time.perf_counter() - started,
        peak_working_set_bytes=peak,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def last_json_line(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("process output contains no JSON object")


def generate_audio(
    path: pathlib.Path, *, seconds: float, sample_rate: int = 44_100
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(seconds * sample_rate))
    partial = path.with_name(f".{path.name}.partial")
    with soundfile.SoundFile(
        partial,
        mode="w",
        samplerate=sample_rate,
        channels=2,
        subtype="PCM_16",
        format="WAV",
    ) as stream:
        block_frames = sample_rate
        for start in range(0, frames, block_frames):
            count = min(block_frames, frames - start)
            index = np.arange(start, start + count, dtype=np.float64)
            time_axis = index / sample_rate
            chirp = np.sin(2.0 * np.pi * (220.0 + 55.0 * time_axis) * time_axis)
            pulse = 0.35 + 0.25 * (np.sin(2.0 * np.pi * 2.0 * time_axis) > 0)
            mono = (0.22 * chirp * pulse).astype(np.float32)
            stream.write(np.stack((mono, mono), axis=1))
    os.replace(partial, path)


def run_worker(worker: pathlib.Path, arguments: list[str]) -> ProcessResult:
    result = run_monitored([str(worker), *arguments])
    if result.returncode != 0:
        raise RuntimeError(
            f"worker failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result


def run_cli(cli: pathlib.Path, arguments: list[str]) -> ProcessResult:
    result = run_monitored([str(cli), *arguments])
    if result.returncode != 0:
        raise RuntimeError(f"CLI failed ({result.returncode}): {result.stderr.strip()}")
    return result


def edit_export(
    worker: pathlib.Path,
    source_result: pathlib.Path,
    output: pathlib.Path,
) -> dict[str, Any]:
    document = json.loads(
        (source_result / "performance.json").read_text(encoding="utf-8")
    )
    if document["notes"]:
        document["notes"][0]["velocity"] = max(
            1, min(127, document["notes"][0]["velocity"] - 7)
        )
    payload = output.parent / f"{output.name}.performance.json"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    message = {
        "protocol_version": 3,
        "job_id": "e2e-edit",
        "type": "start",
        "payload": {
            "operation": "edit_export",
            "input_path": str(payload),
            "staging_dir": str(output),
            "options": {
                "source_result_dir": str(source_result),
                "revision_id": output.name,
                "parent_revision_id": document["revision"]["id"],
                "preview_wav": True,
                "title": source_result.name,
            },
        },
    }
    process = subprocess.Popen(
        [str(worker)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    terminal: dict[str, Any] | None = None
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            break
        value = json.loads(line)
        if value.get("type") in {"result", "error", "cancelled"}:
            terminal = value
            break
    process.stdin.close()
    process.wait(timeout=30)
    if process.returncode != 0 or terminal is None or terminal.get("type") != "result":
        raise RuntimeError(f"edit export failed: {terminal}")
    return terminal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=pathlib.Path, required=True)
    parser.add_argument("--worker", type=pathlib.Path, required=True)
    parser.add_argument("--component", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--short-seconds", type=float, default=2.0)
    parser.add_argument("--long-seconds", type=float, default=600.0)
    args = parser.parse_args()

    cli = args.cli.expanduser().resolve()
    worker = args.worker.expanduser().resolve()
    output = args.output.expanduser().resolve()
    workspace = pathlib.Path(__file__).resolve().parents[1]
    if output == workspace or output.parent == output:
        raise ValueError(f"unsafe E2E output directory: {output}")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    short_audio = output / "short.wav"
    long_audio = output / "long-10m.wav"
    generate_audio(short_audio, seconds=args.short_seconds)
    generate_audio(long_audio, seconds=args.long_seconds)

    analysis_first = run_worker(
        worker,
        [
            "analyze",
            "--input",
            str(long_audio),
            "--output",
            str(output / "long-analysis"),
            "--spectral",
        ],
    )
    analysis_second = run_worker(
        worker,
        [
            "analyze",
            "--input",
            str(long_audio),
            "--output",
            str(output / "long-analysis"),
            "--spectral",
        ],
    )
    analysis_document = last_json_line(analysis_first.stdout)
    cache_document = last_json_line(analysis_second.stdout)

    transcription = run_cli(
        cli,
        [
            "transcribe",
            str(short_audio),
            "--output",
            str(output / "short-result"),
            "--worker",
            str(worker),
            "--preview-wav",
            "--json",
        ],
    )
    transcription_document = last_json_line(transcription.stdout)
    short_result = output / "short-result"
    required = (
        "source.mid",
        "cleaned.mid",
        "mapped.mid",
        "performance.json",
        "performance.mid",
        "score.events.json",
        "score.readable.txt",
        "score.compat.txt",
        "preview.wav",
        "report.json",
    )
    missing = [name for name in required if not (short_result / name).is_file()]
    if missing:
        raise RuntimeError(f"short workflow missing artifacts: {missing}")

    parent_performance_bytes = (short_result / "performance.json").read_bytes()
    parent_performance = json.loads(parent_performance_bytes)
    edit_export(worker, short_result, output / "edit-01")
    edited_performance = json.loads(
        (output / "edit-01" / "performance.json").read_text(encoding="utf-8")
    )
    edit_parent_unchanged = (
        short_result / "performance.json"
    ).read_bytes() == parent_performance_bytes
    edit_velocity_changed = (
        not parent_performance["notes"]
        or edited_performance["notes"][0]["velocity"]
        != parent_performance["notes"][0]["velocity"]
    )
    events = json.loads(
        (output / "edit-01" / "score.events.json").read_text(encoding="utf-8")
    )
    expected_onsets = sorted({note["start_us"] for note in edited_performance["notes"]})
    actual_onsets = [event["at_us"] for event in events["events"]]
    edit_consistent = expected_onsets == actual_onsets

    separation: dict[str, Any] | None = None
    routing: dict[str, Any] | None = None
    if args.component is not None:
        separation = asdict(
            run_worker(
                worker,
                [
                    "separate",
                    "--component",
                    str(args.component.expanduser().resolve()),
                    "--input",
                    str(short_audio),
                    "--output",
                    str(output / "stems"),
                    "--model",
                    "htdemucs",
                ],
            )
        )
        routing = asdict(
            run_worker(
                worker,
                [
                    "route",
                    "--stem-set",
                    str(output / "stems" / "stem-set-v1.json"),
                    "--output",
                    str(output / "routing"),
                    "--mode",
                    "two_voice",
                ],
            )
        )

    peak_mib = round(
        max(
            analysis_first.peak_working_set_bytes,
            analysis_second.peak_working_set_bytes,
        )
        / 1024
        / 1024,
        2,
    )
    gate_failures: list[str] = []
    if analysis_first.returncode != 0:
        gate_failures.append("long analysis failed")
    if analysis_first.elapsed_seconds > 120:
        gate_failures.append("long analysis exceeded 120 seconds")
    if not cache_document.get("cache_hit"):
        gate_failures.append("analysis cache was not reused")
    if analysis_second.elapsed_seconds > 60:
        gate_failures.append("cache reload exceeded 60 seconds")
    if peak_mib > 2048:
        gate_failures.append("analysis peak working set exceeded 2 GiB")
    if missing:
        gate_failures.append(f"short workflow missing artifacts: {missing}")
    if not (edit_consistent and edit_velocity_changed and edit_parent_unchanged):
        gate_failures.append("edit export was inconsistent with the parent performance")

    report = {
        "format_version": 1,
        "long_audio": {
            "seconds": args.long_seconds,
            "first_elapsed_seconds": analysis_first.elapsed_seconds,
            "first_peak_working_set_bytes": analysis_first.peak_working_set_bytes,
            "analysis_cache_hit": bool(cache_document.get("cache_hit")),
            "second_elapsed_seconds": analysis_second.elapsed_seconds,
            "second_peak_working_set_bytes": analysis_second.peak_working_set_bytes,
            "frames": analysis_document.get("frames"),
        },
        "short_e2e": {
            "artifacts": sorted(required),
            "event_count": len(events["events"]),
            "edit_consistent": edit_consistent,
            "edit_velocity_changed": edit_velocity_changed,
            "parent_unchanged": edit_parent_unchanged,
            "cli_result": "result",
            "transcription_artifacts": len(
                transcription_document["result"]["artifacts"]
            ),
        },
        "separation": separation,
        "routing": routing,
        "gate": {
            "analysis_completed": analysis_first.returncode == 0,
            "cache_hit": bool(cache_document.get("cache_hit")),
            "short_workflow_completed": not missing,
            "edit_export_consistent": edit_consistent and edit_velocity_changed,
            "peak_working_set_mib": peak_mib,
            "failures": gate_failures,
        },
    }
    report_path = output / "e2e-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(report_path)
    if gate_failures:
        for failure in gate_failures:
            print(f"GATE FAILURE: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
