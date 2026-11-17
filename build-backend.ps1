$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

# The sidecar filename is tied to the Rust target triple.  Let rustc tell us
# which one this machine builds, so the same script works on another PC.
$hostLine = (& rustc -vV | Select-String '^host:').Line
if (-not $hostLine) { throw "Could not determine the Rust host triple" }
$triple = $hostLine.Substring($hostLine.IndexOf(':') + 1).Trim()

$distDir = Join-Path $PSScriptRoot "dist-backend"
$workDir = Join-Path $PSScriptRoot "build-backend"
$binariesDir = Join-Path $PSScriptRoot "src-tauri\binaries"
$targetExe = Join-Path $binariesDir "acc-backend-$triple.exe"

New-Item -ItemType Directory -Force -Path $binariesDir | Out-Null

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name acc-backend `
    --distpath $distDir `
    --workpath $workDir `
    --specpath $workDir `
    --icon (Join-Path $PSScriptRoot "app.ico") `
    --version-file (Join-Path $PSScriptRoot "version_info.txt") `
    --collect-all uiautomation `
    --collect-all comtypes `
    --add-data "$PSScriptRoot\LICENSE.txt;." `
    --add-data "$PSScriptRoot\AppLogo.jpeg;." `
    (Join-Path $PSScriptRoot "backend_entry.py")

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$builtExe = Join-Path $distDir "acc-backend.exe"
if (-not (Test-Path -LiteralPath $builtExe)) {
    throw "PyInstaller did not produce $builtExe"
}

Copy-Item -LiteralPath $builtExe -Destination $targetExe -Force

Write-Host ""
Write-Host "Backend sidecar built:"
Write-Host "  $targetExe"
