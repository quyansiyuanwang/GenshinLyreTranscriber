"""Minimal Basic Pitch ONNX smoke interface used by the portable worker build."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import pathlib
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

MODEL_SIZE = 230_444
MODEL_SHA256 = "2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec"
MODEL_RELATIVE_PATH = pathlib.Path("glt_core/resources/models/basic_pitch/nmp.onnx")
MODEL_ENV_VAR = "GLT_BASIC_PITCH_MODEL"


class ModelResourceError(RuntimeError):
    """Raised when the bundled or explicitly selected model cannot be trusted."""


@dataclass(frozen=True)
class ModelInfo:
    path: str
    model_type: str
    providers: tuple[str, ...]
    onnxruntime_version: str


@dataclass(frozen=True)
class TranscriptionInfo:
    input_path: str
    output_path: str
    model: ModelInfo
    note_count: int
    pitch_min: int | None
    pitch_max: int | None
    model_load_seconds: float
    inference_seconds: float
    peak_working_set_bytes: int | None
    output_bytes: int

    def to_json_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["model"]["providers"] = list(self.model.providers)
        return value


def _installed_model_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / "resources/models/basic_pitch/nmp.onnx"


def _frozen_model_path() -> pathlib.Path:
    return pathlib.Path(getattr(sys, "_MEIPASS", "")) / MODEL_RELATIVE_PATH


def _peak_working_set_bytes() -> int | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL
    process = get_current_process()
    if not get_process_memory_info(process, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return int(counters.PeakWorkingSetSize)


def resolve_model_path(explicit_path: pathlib.Path | str | None = None) -> pathlib.Path:
    """Resolve and verify the pinned Basic Pitch ONNX model."""
    candidates: list[pathlib.Path] = []
    if explicit_path is not None:
        candidates.append(pathlib.Path(explicit_path))

    configured_path = os.environ.get(MODEL_ENV_VAR)
    if configured_path:
        candidates.append(pathlib.Path(configured_path))

    frozen_path = _frozen_model_path()
    if getattr(sys, "frozen", False):
        candidates.append(frozen_path)

    candidates.append(_installed_model_path())

    checked: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        location = str(resolved)
        if location in seen:
            continue
        seen.add(location)
        checked.append(location)
        if not resolved.is_file():
            continue
        if resolved.stat().st_size != MODEL_SIZE:
            raise ModelResourceError(
                f"Model size mismatch for {resolved}: expected {MODEL_SIZE} bytes"
            )
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if digest != MODEL_SHA256:
            raise ModelResourceError(f"Model SHA256 mismatch for {resolved}: {digest}")
        return resolved

    locations = ", ".join(checked)
    raise ModelResourceError(f"Basic Pitch ONNX model not found. Checked: {locations}")


def _load_basic_pitch_api() -> tuple[Any, Any, Any, Any]:
    previous_disable = logging.root.manager.disable
    logging.disable(logging.WARNING)
    try:
        import onnxruntime
        from basic_pitch.constants import AUDIO_SAMPLE_RATE, FFT_HOP
        from basic_pitch.inference import Model, run_inference
        from basic_pitch.note_creation import model_output_to_notes
    finally:
        logging.disable(previous_disable)
    return Model, run_inference, model_output_to_notes, (onnxruntime, AUDIO_SAMPLE_RATE, FFT_HOP)


def inspect_model(model_path: pathlib.Path | str | None = None) -> tuple[Any, ModelInfo]:
    """Load the model and require the CPU-only ONNX Runtime execution provider."""
    resolved = resolve_model_path(model_path)
    Model, _run_inference, _model_output_to_notes, runtime = _load_basic_pitch_api()
    onnxruntime, _sample_rate, _fft_hop = runtime

    model = Model(resolved)
    if model.model_type.name != "ONNX":
        raise RuntimeError(f"Expected ONNX backend, got {model.model_type.name}")
    providers = tuple(model.model.get_providers())
    if providers != ("CPUExecutionProvider",):
        raise RuntimeError(f"Expected CPUExecutionProvider only, got {providers!r}")

    return model, ModelInfo(
        path=str(resolved),
        model_type=model.model_type.name,
        providers=providers,
        onnxruntime_version=onnxruntime.__version__,
    )


def transcribe_file(
    input_path: pathlib.Path | str,
    output_path: pathlib.Path | str,
    *,
    model_path: pathlib.Path | str | None = None,
) -> TranscriptionInfo:
    """Transcribe one local audio file to MIDI using the pinned ONNX model."""
    source = pathlib.Path(input_path).expanduser().resolve()
    destination = pathlib.Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Input audio does not exist: {source}")

    started = time.perf_counter()
    model, model_info = inspect_model(model_path)
    model_loaded = time.perf_counter()
    _model_class, run_inference, model_output_to_notes, runtime = _load_basic_pitch_api()
    _onnxruntime, sample_rate, fft_hop = runtime
    model_output = run_inference(source, model)
    minimum_note_length = 127.70
    min_note_len = int(round(minimum_note_length / 1000 * (sample_rate / fft_hop)))
    midi_data, note_events = model_output_to_notes(
        model_output,
        onset_thresh=0.5,
        frame_thresh=0.3,
        min_note_len=min_note_len,
        min_freq=None,
        max_freq=None,
        multiple_pitch_bends=False,
        melodia_trick=True,
        midi_tempo=120,
    )
    inferred = time.perf_counter()

    valid_times = all(
        math.isfinite(float(start))
        and math.isfinite(float(end))
        and float(start) >= 0
        and float(end) > float(start)
        for start, end, _pitch, _amplitude, _bends in note_events
    )
    if not valid_times:
        raise RuntimeError("Basic Pitch returned an invalid note time range")

    destination.parent.mkdir(parents=True, exist_ok=True)
    midi_data.write(str(destination))
    pitches = [int(event[2]) for event in note_events]
    return TranscriptionInfo(
        input_path=str(source),
        output_path=str(destination),
        model=model_info,
        note_count=len(note_events),
        pitch_min=min(pitches) if pitches else None,
        pitch_max=max(pitches) if pitches else None,
        model_load_seconds=round(model_loaded - started, 6),
        inference_seconds=round(inferred - model_loaded, 6),
        peak_working_set_bytes=_peak_working_set_bytes(),
        output_bytes=destination.stat().st_size,
    )
