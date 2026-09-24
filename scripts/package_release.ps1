param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[a-z0-9][a-z0-9.-]*$")]
    [string]$AssetPrefix,

    [string]$OutputDirectory = "artifacts/release"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Copy-PackageDocs {
    param([string]$Destination)
    Copy-Item (Join-Path $Root "LICENSE") $Destination
    Copy-Item (Join-Path $Root "README.md") $Destination
    Copy-Item (Join-Path $Root "docs") $Destination -Recurse -Force
}

function Write-FileHashList {
    param([string]$Directory, [string]$Destination)
    Get-ChildItem -LiteralPath $Directory -Recurse -File |
        Where-Object { $_.Name -ne "SHA256SUMS" } |
        Sort-Object FullName |
        Get-FileHash -Algorithm SHA256 |
        ForEach-Object {
            $relative = $_.Path.Substring($Directory.Length + 1).Replace("\", "/")
            "$($_.Hash.ToLowerInvariant())  $relative"
        } |
        Set-Content -LiteralPath $Destination -Encoding ascii
}

Push-Location $Root
try {
    ./scripts/build_worker.ps1
    cargo build --release --locked
    if ($LASTEXITCODE -ne 0) {
        throw "Rust release build failed with exit code $LASTEXITCODE"
    }
    pnpm --dir crates/glt-gui tauri build
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri bundle build failed with exit code $LASTEXITCODE"
    }

    $ReleaseRoot = Join-Path $Root $OutputDirectory
    $RootFull = [IO.Path]::GetFullPath($Root).TrimEnd("\") + "\"
    $ReleaseRootFull = [IO.Path]::GetFullPath($ReleaseRoot)
    if (-not $ReleaseRootFull.StartsWith($RootFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "release output escapes workspace: $ReleaseRootFull"
    }
    New-Item -ItemType Directory -Path $ReleaseRoot -Force | Out-Null

    $CliStage = Join-Path $ReleaseRoot ".cli-stage"
    $GuiStage = Join-Path $ReleaseRoot ".gui-stage"
    foreach ($directory in @($CliStage, $GuiStage)) {
        if (Test-Path -LiteralPath $directory) {
            Remove-Item -LiteralPath $directory -Recurse -Force
        }
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    Copy-Item (Join-Path $Root "target/release/glt.exe") $CliStage
    Copy-Item (Join-Path $Root "artifacts/worker/glt-worker") $CliStage -Recurse -Force
    Copy-PackageDocs $CliStage
    Write-FileHashList -Directory $CliStage -Destination (Join-Path $CliStage "SHA256SUMS")

    $GuiExecutable = Join-Path $Root "target/release/glt-gui.exe"
    if (-not (Test-Path -LiteralPath $GuiExecutable -PathType Leaf)) {
        throw "Tauri executable is missing: $GuiExecutable"
    }
    Copy-Item $GuiExecutable (Join-Path $GuiStage "GenshinLyreTranscriber.exe")
    Copy-Item (Join-Path $Root "artifacts/worker/glt-worker") $GuiStage -Recurse -Force
    Copy-PackageDocs $GuiStage
    Write-FileHashList -Directory $GuiStage -Destination (Join-Path $GuiStage "SHA256SUMS")

    $CliZip = Join-Path $ReleaseRoot "$AssetPrefix-cli-tui-windows-x64.zip"
    $GuiZip = Join-Path $ReleaseRoot "$AssetPrefix-gui-windows-x64.zip"
    Compress-Archive -Path (Join-Path $CliStage "*") -DestinationPath $CliZip -Force
    Compress-Archive -Path (Join-Path $GuiStage "*") -DestinationPath $GuiZip -Force

    $Installer = Get-ChildItem (Join-Path $Root "target/release/bundle/nsis") -Filter "*.exe" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $Installer) {
        throw "Tauri NSIS installer was not produced"
    }
    $InstallerAsset = Join-Path $ReleaseRoot "$AssetPrefix-gui-windows-x64-setup.exe"
    Copy-Item $Installer.FullName $InstallerAsset -Force

    $Assets = @($CliZip, $GuiZip, $InstallerAsset)
    $HashLines = foreach ($asset in $Assets) {
        $hash = (Get-FileHash -LiteralPath $asset -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $([IO.Path]::GetFileName($asset))"
    }
    $HashLines | Set-Content (Join-Path $ReleaseRoot "$AssetPrefix-SHA256SUMS") -Encoding ascii
    $HashLines
}
finally {
    Pop-Location
}
