"""Application entry point."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes

import customtkinter as ctk

from .autostart import (
    cleanup_legacy_run_entry,
    is_enabled as startup_is_enabled,
    set_enabled as set_startup_enabled,
)
from .config import ConfigStore
from .gui import App
from .logging_setup import setup_logging
from .resources import icon_path

log = logging.getLogger(__name__)

APP_USER_MODEL_ID = "KutaOS.AutoClaudeContinue"
_mutex_handle = None


def _enable_dpi_awareness() -> None:
    try:
        # Per-monitor DPI awareness (Windows 8.1+).  Fall back silently.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _set_app_user_model_id() -> None:
    """Give the process an identity so the taskbar/tray group it by name."""

    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        log.debug("Could not set the AppUserModelID", exc_info=True)


def _single_instance() -> bool:
    """Return True if this is the only running instance."""

    global _mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, "AutoClaudeContinue_SingleInstance")
    if not handle:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def _install_exception_hooks() -> None:
    """Send otherwise-invisible pythonw/Tk errors to the log file."""

    logger = logging.getLogger("autoclaude_continue")

    def handle(exc_type, exc_value, exc_traceback) -> None:
        logger.critical(
            "Uncaught exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    sys.excepthook = handle
    threading.excepthook = lambda args: handle(
        args.exc_type,
        args.exc_value,
        args.exc_traceback,
    )


def main() -> int:
    _enable_dpi_awareness()
    _set_app_user_model_id()
    setup_logging()
    _install_exception_hooks()
    from . import __version__

    log.info("Starting AutoClaudeContinue %s", __version__)

    if not _single_instance():
        ctypes.windll.user32.MessageBoxW(
            None,
            "AutoClaudeContinue is already running.\n"
            "Look for its tray icon or window.",
            "AutoClaudeContinue",
            0x40,
        )
        return 0

    store = ConfigStore()
    config = store.get()

    # Remove the old HKCU Run value so Task Manager stops listing pythonw.exe
    # as a startup app.  Create the Startup-folder shortcut only when the user
    # has asked for it and it is missing.
    cleanup_legacy_run_entry()
    try:
        if config.start_with_windows and not startup_is_enabled():
            set_startup_enabled(True)
    except Exception:
        log.exception("Could not sync the start-with-Windows setting")

    try:
        root = ctk.CTk()
        root.withdraw()
    except Exception as exc:
        log.exception("Could not create the GUI")
        ctypes.windll.user32.MessageBoxW(
            None,
            f"Could not start the GUI:\n{exc}",
            "AutoClaudeContinue",
            0x10,
        )
        return 1

    try:
        App(root, store)
        # CustomTkinter can ignore deiconify() before the event loop starts.
        root.after(50, root.deiconify)
        root.mainloop()
    except Exception as exc:
        log.exception("Unhandled GUI error")
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                f"AutoClaudeContinue encountered an error:\n{exc}\n\n"
                "See the log at %APPDATA%\\AutoClaudeContinue\\app.log",
                "AutoClaudeContinue",
                0x10,
            )
        except Exception:
            pass
        return 1
    finally:
        log.info("Exiting AutoClaudeContinue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
