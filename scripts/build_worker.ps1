$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Model = Join-Path $Root "python/src/glt_core/resources/models/basic_pitch/nmp.onnx"

Push-Location $Root
try {
    if (-not (Test-Path -LiteralPath $Model -PathType Leaf)) {
        uv run --project python python scripts/fetch_resources.py basic-pitch
        if ($LASTEXITCODE -ne 0) {
            throw "Resource download failed with exit code $LASTEXITCODE"
        }
    }

    uv run --project python pyinstaller `
        --clean `
        --noconfirm `
        --distpath artifacts/worker `
        --workpath build/pyinstaller `
        python/glt-worker.spec
    if ($LASTEXITCODE -ne 0) {
        throw "Worker build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
