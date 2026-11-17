"""Image attachments: validation, clipboard capture, and thumbnails."""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from .resources import data_dir

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MAX_ATTACHMENTS = 8


def attachment_dir() -> Path:
    path = data_dir() / "attachments"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def format_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def save_clipboard_images() -> list[str]:
    """Save images from the clipboard and return their paths.

    Explorer file copies are used directly; a bitmap on the clipboard is saved
    as a PNG in the app's attachment folder.
    """

    destination = attachment_dir()
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$files = [System.Windows.Forms.Clipboard]::GetFileDropList();"
        "if ($files -and $files.Count -gt 0) {"
        "  foreach ($file in $files) { Write-Output (\"FILE`t\" + $file) };"
        "  exit 0"
        "};"
        "$img = [System.Windows.Forms.Clipboard]::GetImage();"
        "if ($img -ne $null) {"
        "  $name = 'pasted-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.png';"
        "  $path = Join-Path $env:ACC_DEST $name;"
        "  $img.Save($path, [System.Drawing.Imaging.ImageFormat]::Png);"
        "  $img.Dispose();"
        "  Write-Output (\"FILE`t\" + $path)"
        "};"
    )
    env = dict(os.environ)
    env["ACC_DEST"] = str(destination)
    try:
        result = subprocess.run(
            ["powershell", "-STA", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=25,
            creationflags=0x08000000,
            env=env,
        )
        if result.returncode != 0:
            log.debug("Clipboard read failed: %s", result.stderr.strip())
            return []
    except Exception:
        log.debug("Could not read the clipboard", exc_info=True)
        return []

    paths: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("FILE\t"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        path = Path(raw)
        if path.is_file() and is_image(path):
            paths.append(str(path))
    return paths


def unique_copy(path: str) -> str:
    """Return a non-conflicting path for a pasted image."""

    source = Path(path)
    if not source.is_file():
        return path
    if source.parent == attachment_dir():
        return path
    target = attachment_dir() / f"{datetime.now():%Y%m%d-%H%M%S}-{source.name}"
    try:
        target.write_bytes(source.read_bytes())
        return str(target)
    except OSError:
        return path
