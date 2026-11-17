"""Identify the executable that owns a top-level window.

Both Claude Desktop and the ChatGPT desktop app use Chromium's window class
``Chrome_WidgetWin_1``, which browsers also use.  Matching only on the window
class and title lets an Opera or Chrome tab called "Claude Just Got a HUGE
Upgrade" masquerade as the Claude app.  The owning process name is the
reliable discriminator.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PATH = 1024

_user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD

_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE

_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def window_process_name(hwnd: int) -> str:
    """Return the lower-case executable name owning *hwnd*, or ``""``."""

    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    try:
        _user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    except Exception:
        return ""
    if not pid.value:
        return ""
    handle = _kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
    )
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(_MAX_PATH)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return ""
        return Path(buffer.value).name.lower()
    except Exception:
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def window_belongs_to(hwnd: int, executable: str) -> bool:
    """True when *hwnd* belongs to *executable*.

    An unreadable process name (permissions, a closing window) is treated as a
    match so a lookup failure never hides the real app; the caller still
    applies its title/class checks.
    """

    name = window_process_name(hwnd)
    if not name:
        return True
    return name == executable.lower()
