from pathlib import Path

project_dir = Path(SPECPATH)
source_dir = project_dir / "src"
model_path = source_dir / "glt_core/resources/models/basic_pitch/nmp.onnx"
if not model_path.is_file():
    raise SystemExit(
        "Missing pinned model. Run scripts/fetch_resources.py basic-pitch before building."
    )

schema_dir = project_dir.parent / "schemas"
schema_files = sorted(schema_dir.glob("*.schema.json"))
if len(schema_files) != 7:
    raise SystemExit(f"Expected 7 protocol schemas, found {len(schema_files)}")

datas = [
    (str(model_path), "glt_core/resources/models/basic_pitch"),
    *[(str(schema), "glt_core/schemas") for schema in schema_files],
]

a = Analysis(
    [str(source_dir / "glt_core/worker.py")],
    pathex=[str(source_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "coremltools",
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
    name="glt-worker",
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
    name="glt-worker",
)
