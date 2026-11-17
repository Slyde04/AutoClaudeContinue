"""Paths and generated assets for the app window, tray, and install shortcut."""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

APP_ICON_NAME = "app.ico"
FONT_FAMILY = "OpenDyslexic3"
FONT_FILES = ("OpenDyslexic3-Regular.ttf", "OpenDyslexic3-Bold.ttf")
FR_PRIVATE = 0x10


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            return Path(bundle)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    from .config import default_data_dir

    path = default_data_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def resource_path(name: str) -> Path:
    root = project_root()
    direct = root / name
    if direct.exists():
        return direct
    bundled = root / "assets" / name
    if bundled.exists():
        return bundled
    return direct


def logo_path() -> Path | None:
    for name in ("AppLogo.jpeg", "AppLogo.jpg", "AppLogo.png", "app-logo.png"):
        path = resource_path(name)
        if path.exists():
            return path
    return None


def license_path() -> Path | None:
    for name in ("LICENSE.txt", "LICENSE.md", "LICENSE"):
        path = resource_path(name)
        if path.exists():
            return path
    return None


def font_paths() -> list[Path]:
    return [resource_path(name) for name in FONT_FILES]


def register_fonts() -> bool:
    """Load the bundled OpenDyslexic faces for this process only.

    Tk cannot read a .ttf by path, so the files are registered privately with
    GDI before the first widget is created.  Nothing is installed system-wide.
    """

    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.AddFontResourceExW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    gdi32.AddFontResourceExW.restype = ctypes.c_int

    loaded = False
    for path in font_paths():
        if not path.exists():
            continue
        try:
            if gdi32.AddFontResourceExW(str(path), FR_PRIVATE, None):
                loaded = True
                log.info("Loaded font %s", path.name)
            else:
                log.warning("Could not load font %s", path)
        except Exception:
            log.debug("Could not register %s", path, exc_info=True)
    return loaded


def youtube_logo_path() -> Path | None:
    path = resource_path("youtube.png")
    return path if path.exists() else None


def discord_logo_path() -> Path | None:
    path = resource_path("discord.png")
    return path if path.exists() else None


def icon_path() -> Path | None:
    """Return a usable .ico, generating one from the logo when needed."""

    frozen = getattr(sys, "frozen", False)
    if frozen:
        for name in (APP_ICON_NAME, "AppLogo.ico"):
            path = resource_path(name)
            if path.exists():
                return path

    source = logo_path()
    if source is None:
        return None

    target = data_dir() / APP_ICON_NAME
    try:
        if (
            target.exists()
            and target.stat().st_mtime >= source.stat().st_mtime
            and target.stat().st_size > 0
        ):
            return target
    except OSError:
        pass

    try:
        from PIL import Image

        with Image.open(source) as image:
            image = image.convert("RGBA")
            side = min(image.size)
            left = (image.width - side) // 2
            top = (image.height - side) // 2
            image = image.crop((left, top, left + side, top + side))
            image.save(
                target,
                format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )
        return target
    except Exception:
        log.debug("Could not generate the app icon", exc_info=True)
        return None


def startup_shortcut_path() -> Path:
    import os

    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata)
    else:
        base = Path.home() / "AppData" / "Roaming"
    return (
        base
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "AutoClaudeContinue.lnk"
    )
