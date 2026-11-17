"""Manage the Startup-folder shortcut used for "start with Windows".

A shortcut named ``AutoClaudeContinue.lnk`` makes Task Manager show the app by
name instead of the generic ``pythonw.exe`` entry produced by the old HKCU Run
key.  When a packaged exe exists it is used as the shortcut target.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import winreg
from pathlib import Path

from .resources import icon_path, project_root, startup_shortcut_path

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AutoClaudeContinue"
SHORTCUT_NAME = "AutoClaudeContinue"
SHORTCUT_DESCRIPTION = "AutoClaudeContinue - automatic rate-limit continue"


def _packaged_exe() -> Path | None:
    root = project_root()
    for candidate in (
        root / "AutoClaudeContinue.exe",
        root / "dist" / "AutoClaudeContinue" / "AutoClaudeContinue.exe",
        root / "dist" / "AutoClaudeContinue.exe",
    ):
        if candidate.exists():
            return candidate
    return None


def startup_target() -> tuple[Path, str, Path]:
    """Return ``(target, arguments, working_directory)`` for the shortcut."""

    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        return exe, "", exe.parent

    packaged = _packaged_exe()
    if packaged is not None:
        exe = packaged.resolve()
        return exe, "", exe.parent

    root = project_root()
    script = root / "run.pyw"
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = exe
    return pythonw.resolve(), f'"{script}"', root


def startup_command() -> str:
    target, arguments, _workdir = startup_target()
    if arguments:
        return f'"{target}" {arguments}'
    return f'"{target}"'


def is_enabled() -> bool:
    return startup_shortcut_path().exists()


def set_enabled(enabled: bool) -> bool:
    _remove_run_value()
    shortcut = startup_shortcut_path()
    if not enabled:
        try:
            shortcut.unlink()
            log.info("Removed the startup shortcut")
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            log.warning("Could not remove the startup shortcut: %s", exc)
            return False

    target, arguments, workdir = startup_target()
    icon = icon_path()
    if icon is None:
        icon = target

    try:
        shortcut.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Could not create the Startup folder: %s", exc)
        return False

    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$s = $ws.CreateShortcut($env:ACC_LNK); "
        "$s.TargetPath = $env:ACC_TARGET; "
        "$s.Arguments = $env:ACC_ARGS; "
        "$s.WorkingDirectory = $env:ACC_WORKDIR; "
        "$s.IconLocation = $env:ACC_ICON; "
        "$s.Description = $env:ACC_DESC; "
        "$s.WindowStyle = 1; "
        "$s.Save();"
    )
    env = dict(os.environ)
    env.update(
        {
            "ACC_LNK": str(shortcut),
            "ACC_TARGET": str(target),
            "ACC_ARGS": arguments,
            "ACC_WORKDIR": str(workdir),
            "ACC_ICON": str(icon),
            "ACC_DESC": SHORTCUT_DESCRIPTION,
        }
    )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=0x08000000,
            env=env,
        )
        if result.returncode != 0:
            log.warning("Could not create the startup shortcut: %s", result.stderr.strip())
            return False
        log.info("Created the startup shortcut at %s", shortcut)
        return shortcut.exists()
    except Exception:
        log.exception("Could not create the startup shortcut")
        return False


def _remove_run_value() -> None:
    """Remove the legacy HKCU Run value if an older build created one."""

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
                log.info("Removed the legacy Run key entry")
            except FileNotFoundError:
                pass
    except OSError:
        pass


def cleanup_legacy_run_entry() -> None:
    """Public hook used at startup to remove the old Run-key entry."""

    _remove_run_value()
