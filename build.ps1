$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

# 1. Package the Python worker as the Tauri sidecar.
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build-backend.ps1")
if ($LASTEXITCODE -ne 0) { throw "The backend sidecar build failed" }

# 2. Build the Rust shell and the NSIS installer.
npm install
if ($LASTEXITCODE -ne 0) { throw "npm install failed" }

npm run build
if ($LASTEXITCODE -ne 0) { throw "Tauri build failed" }

$installer = Join-Path $PSScriptRoot "src-tauri\target\release\bundle\nsis"
Write-Host ""
Write-Host "Built the AutoClaudeContinue installer under:"
Write-Host "  $installer"
