from pathlib import Path

project_dir = Path(SPECPATH)
source_dir = project_dir / "src"

a = Analysis(
    [str(source_dir / "glt_core/separation/demucs_worker.py")],
    pathex=[str(source_dir)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "demucs.apply",
        "demucs.audio",
        "demucs.hf",
        "demucs.pretrained",
        "demucs.repo",
        "demucs.separate",
        "demucs.states",
        "demucs.transformer",
        "torchaudio",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "mypy",
        "pandas",
        "pytest",
        "pygments",
        "ruff",
        "sklearn",
        "tensorflow",
        "tflite_runtime",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="glt-separator-worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="glt-separator-worker",
)
