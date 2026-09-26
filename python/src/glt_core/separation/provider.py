"""Execute a verified separator component and validate its stem set."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

from glt_core.separation.component import verify_component
from glt_core.separation.stem_set import StemSet, validate_stem_set


class SeparationError(RuntimeError):
    """Stable source-separation failure."""


class SeparationCancelled(SeparationError):
    """Raised when component separation is cancelled."""


@dataclass(frozen=True, slots=True)
class SeparationRun:
    stem_set: StemSet
    output_directory: pathlib.Path
    elapsed_seconds: float


def run_component(
    component_directory: pathlib.Path | str,
    input_path: pathlib.Path | str,
    output_directory: pathlib.Path | str,
    *,
    model_id: str,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[str, float | None], None] | None = None,
) -> SeparationRun:
    component = verify_component(component_directory, model_id=model_id)
    source = pathlib.Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise SeparationError(f"input audio does not exist: {source}")
    output = pathlib.Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    command = list(component.command)
    if pathlib.Path(command[0]).suffix.lower() == ".py":
        command.insert(0, sys.executable)
    command.extend(
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--model",
            model_id,
        ]
    )
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
        cwd=component.directory,
    )
    if process.stdout is None:
        process.kill()
        raise SeparationError("separator stdout is unavailable")
    result_payload: dict[str, object] | None = None
    stderr_chunks: list[str] = []

    def drain_stderr() -> None:
        if process.stderr is not None:
            stderr_chunks.append(process.stderr.read())

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    try:
        for line in process.stdout:
            if cancelled is not None and cancelled():
                process.kill()
                raise SeparationCancelled("separation was cancelled")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "progress" and progress is not None:
                fraction = payload.get("fraction")
                progress(
                    str(payload.get("stage", "separating")),
                    float(fraction) if isinstance(fraction, (int, float)) else None,
                )
            elif payload.get("type") == "result":
                result_payload = payload
        return_code = process.wait()
        stderr_thread.join(timeout=1)
        stderr = "".join(stderr_chunks)
        if return_code != 0:
            raise SeparationError(stderr.strip() or f"separator exited with {return_code}")
        if result_payload is None:
            raise SeparationError("separator did not return a result")
        actual_model_hash = result_payload.get("model_sha256")
        if component.model is not None and actual_model_hash != component.model.logical_sha256:
            raise SeparationError("separator model hash does not match component manifest")
        stem_set_path = result_payload.get("stem_set_path")
        if not isinstance(stem_set_path, str):
            raise SeparationError("separator result has no stem_set_path")
        path = pathlib.Path(stem_set_path).expanduser().resolve()
        if not path.is_relative_to(output):
            raise SeparationError("separator stem set escapes output directory")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SeparationError(f"cannot read separator stem set: {exc}") from exc
        stem_set = validate_stem_set(document, output)
        elapsed_value = result_payload.get("elapsed_seconds", 0.0)
        elapsed = float(elapsed_value) if isinstance(elapsed_value, (int, float)) else 0.0
        return SeparationRun(stem_set=stem_set, output_directory=output, elapsed_seconds=elapsed)
    except BaseException:
        if process.poll() is None:
            process.kill()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
