"""Windows input-safety helpers shared by the Claude and ChatGPT adapters.

The app uses UI Automation patterns for almost everything.  A few actions still
need real input (pasting an image, dismissing a modal with Escape), so this
module keeps those actions predictable:

* modifier keys are always released, even when an action fails;
* the session lock and user idle time can be checked before stealing focus;
* clipboard images are set through PowerShell, avoiding a pywin32 dependency.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.keybd_event.argtypes = [
    ctypes.c_ubyte,
    ctypes.c_ubyte,
    ctypes.c_uint,
    ctypes.c_size_t,
]
_user32.keybd_event.restype = None
_user32.GetLastInputInfo.argtypes = [ctypes.c_void_p]
_user32.GetLastInputInfo.restype = wintypes.BOOL
_kernel32.GetTickCount64.argtypes = []
_kernel32.GetTickCount64.restype = ctypes.c_ulonglong
_user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_user32.OpenInputDesktop.restype = wintypes.HANDLE
_user32.CloseDesktop.argtypes = [wintypes.HANDLE]
_user32.CloseDesktop.restype = wintypes.BOOL
_user32.GetUserObjectInformationW.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_user32.GetUserObjectInformationW.restype = wintypes.BOOL

_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_SHIFT = 0x10
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C
_KEYEVENTF_KEYUP = 0x0002
_UOI_NAME = 2
_DESKTOP_READOBJECTS = 0x0001


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def release_modifiers() -> None:
    """Release every modifier key synthetic input might have left down."""

    for vk in (_VK_CONTROL, _VK_MENU, _VK_SHIFT, _VK_LWIN, _VK_RWIN):
        try:
            _user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
        except Exception:
            pass


def user_idle_seconds() -> float | None:
    """Return seconds since the last keyboard or mouse input, if available."""

    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    try:
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = int(_kernel32.GetTickCount64()) & 0xFFFFFFFF
        elapsed = (now - int(info.dwTime)) & 0xFFFFFFFF
        return elapsed / 1000.0
    except Exception:
        return None


def session_locked() -> bool:
    """Best-effort detection of the Windows lock screen / secure desktop."""

    handle = _user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not handle:
        # Access denied usually means the secure desktop is active.  Any other
        # failure is treated as "unknown, keep working" rather than freezing
        # the app forever.
        return ctypes.get_last_error() == 5
    try:
        size = wintypes.DWORD(0)
        _user32.GetUserObjectInformationW(
            handle, _UOI_NAME, None, 0, ctypes.byref(size)
        )
        if not size.value:
            return False
        buffer = ctypes.create_unicode_buffer(max(2, size.value // ctypes.sizeof(ctypes.c_wchar)))
        if not _user32.GetUserObjectInformationW(
            handle,
            _UOI_NAME,
            buffer,
            size.value,
            ctypes.byref(size),
        ):
            return False
        return buffer.value.strip().lower() != "default"
    except Exception:
        return False
    finally:
        try:
            _user32.CloseDesktop(handle)
        except Exception:
            pass


def wait_until_unlocked(stop_event, poll_seconds: float = 2.0) -> bool:
    """Block until the session is unlocked or *stop_event* is set."""

    while not stop_event.is_set():
        if not session_locked():
            return True
        if stop_event.wait(poll_seconds):
            return False
    return False


def wait_until_idle(
    stop_event,
    required_seconds: float,
    poll_seconds: float = 2.0,
) -> bool:
    """Block until the user has been idle for *required_seconds*."""

    while not stop_event.is_set():
        idle = user_idle_seconds()
        if idle is None or idle >= required_seconds:
            return True
        if stop_event.wait(min(poll_seconds, max(0.5, required_seconds - idle))):
            return False
    return False


def set_clipboard_image(path: str) -> bool:
    """Put an image file on the Windows clipboard via PowerShell."""

    quoted = "'" + str(path).replace("'", "''") + "'"
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        f"$img = [System.Drawing.Image]::FromFile({quoted});"
        "[System.Windows.Forms.Clipboard]::SetImage($img);"
        "$img.Dispose();"
    )
    try:
        result = subprocess.run(
            ["powershell", "-STA", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=0x08000000,
        )
        if result.returncode != 0:
            log.warning("PowerShell clipboard error: %s", result.stderr.strip())
            return False
        return True
    except Exception:
        log.debug("Could not set the clipboard image", exc_info=True)
        return False


def get_clipboard_text() -> str | None:
    """Return clipboard text, or None when the clipboard holds something else."""

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                "Get-Clipboard -Raw -Format Text",
            ],
            capture_output=True,
            timeout=12,
            creationflags=0x08000000,
        )
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace")
    except Exception:
        return None


def set_clipboard_text(text: str) -> bool:
    """Put text back on the clipboard without leaking it through a command line."""

    if not text:
        return False
    script = (
        "$text = [Console]::In.ReadToEnd(); "
        "Set-Clipboard -Value $text"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=12,
            creationflags=0x08000000,
        )
        return result.returncode == 0
    except Exception:
        log.debug("Could not restore clipboard text", exc_info=True)
        return False


def paste_image(window, composer, path: str, verify) -> bool:
    """Paste an image into *composer* and wait for *verify* to become true.

    ``verify`` is a zero-argument callable that returns True once the app has
    registered the attachment.  Synthetic Ctrl+V is guarded so the Control key
    cannot remain stuck down, and the paste is refused unless the composer
    really owns keyboard focus.  That prevents a stray Ctrl+V from landing in
    whatever window the user has in front.
    """

    if not set_clipboard_image(path):
        return False
    try:
        try:
            if not composer.HasKeyboardFocus:
                composer.SetFocus()
                time.sleep(0.2)
            if not composer.HasKeyboardFocus:
                log.warning("Refusing to paste: the composer does not have focus")
                return False
        except Exception:
            log.debug("Could not verify composer focus", exc_info=True)
            return False

        import uiautomation as auto

        auto.SendKeys("{Ctrl}v", interval=0.1, waitTime=0.5)
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if verify():
                return True
            time.sleep(0.2)
        return False
    except Exception:
        log.debug("Could not paste the image", exc_info=True)
        return False
    finally:
        release_modifiers()


def focus_is_safe(
    *,
    pause_when_locked: bool,
    defer_when_active: bool,
    active_grace_seconds: int,
) -> tuple[bool, str]:
    """Return whether it is safe to take focus, plus a reason when it is not."""

    if pause_when_locked and session_locked():
        return False, "The PC is locked"
    if defer_when_active:
        idle = user_idle_seconds()
        if idle is not None and idle < max(0, active_grace_seconds):
            return False, "You are using the PC"
    return True, ""
