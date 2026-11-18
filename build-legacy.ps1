$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

# Generate the Windows icon from the supplied logo.
python -c "from autoclaude_continue.resources import icon_path; import shutil; shutil.copyfile(icon_path(), 'app.ico')"
if ($LASTEXITCODE -ne 0) { throw "Could not generate app.ico" }

# Remove any stale intermediate executable before building.  The copy that
# PyInstaller writes under build\ cannot run on its own, and an old one left
# behind by a failed build is easy to launch by mistake.
$workExe = Join-Path $PSScriptRoot "build\AutoClaudeContinue\AutoClaudeContinue.exe"
if (Test-Path -LiteralPath $workExe) {
    Remove-Item -LiteralPath $workExe -Force
}

python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name AutoClaudeContinue `
    --icon app.ico `
    --version-file version_info.txt `
    --collect-all customtkinter `
    --collect-all uiautomation `
    --hidden-import pystray._win32 `
    --add-data "AppLogo.jpeg;." `
    --add-data "youtube.png;." `
    --add-data "discord.png;." `
    --add-data "OpenDyslexic3-Regular.ttf;." `
    --add-data "OpenDyslexic3-Bold.ttf;." `
    --add-data "app.ico;." `
    --add-data "LICENSE.txt;." `
    run.py

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# PyInstaller also leaves an executable under build\AutoClaudeContinue\.  That
# copy is an intermediate artifact and cannot run on its own (its _internal
# folder is missing), so remove it before anyone launches the wrong file.
$workExe = Join-Path $PSScriptRoot "build\AutoClaudeContinue\AutoClaudeContinue.exe"
if (Test-Path -LiteralPath $workExe) {
    Remove-Item -LiteralPath $workExe -Force
}

$distExe = Join-Path $PSScriptRoot "dist\AutoClaudeContinue\AutoClaudeContinue.exe"
if (-not (Test-Path -LiteralPath $distExe)) {
    throw "PyInstaller did not produce $distExe"
}

Write-Host ""
Write-Host "Built the legacy CustomTkinter app. Run this file:"
Write-Host "  $distExe"
Write-Host ""
Write-Host "The build\AutoClaudeContinue folder is an intermediate PyInstaller"
Write-Host "work directory. Its executable was removed because it cannot run"
Write-Host "without the _internal folder that only exists under dist\."
