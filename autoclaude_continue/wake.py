"""Windows waitable-timer based scheduler.

The important part is ``fResume=True``: a waitable timer with this flag asks
Windows to wake the machine from sleep when the timer becomes due.  This is what
lets the app send "continue" shortly after a rate limit resets even if the PC
was asleep.

If timer creation fails (unusual), a normal thread sleep is used as a fallback.
That fallback still works while the machine is awake, but it cannot wake it.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from datetime import datetime
from typing import Callable

log = logging.getLogger(__name__)

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_CreateWaitableTimerExW = _k32.CreateWaitableTimerExW
_CreateWaitableTimerExW.argtypes = [
    wintypes.LPVOID,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
]
_CreateWaitableTimerExW.restype = wintypes.HANDLE

_SetWaitableTimer = _k32.SetWaitableTimer
_SetWaitableTimer.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(ctypes.c_longlong),
    wintypes.LONG,
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.BOOL,
]
_SetWaitableTimer.restype = wintypes.BOOL

_CancelWaitableTimer = _k32.CancelWaitableTimer
_CancelWaitableTimer.argtypes = [wintypes.HANDLE]
_CancelWaitableTimer.restype = wintypes.BOOL

_CreateEventW = _k32.CreateEventW
_CreateEventW.argtypes = [
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
_CreateEventW.restype = wintypes.HANDLE

_SetEvent = _k32.SetEvent
_SetEvent.argtypes = [wintypes.HANDLE]
_SetEvent.restype = wintypes.BOOL

_WaitForMultipleObjects = _k32.WaitForMultipleObjects
_WaitForMultipleObjects.argtypes = [
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.BOOL,
    wintypes.DWORD,
]
_WaitForMultipleObjects.restype = wintypes.DWORD

_CloseHandle = _k32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL

TIMER_ALL_ACCESS = 0x1F0003
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF
CREATE_WAITABLE_TIMER_MANUAL_RESET = 0x00000001


class WakeScheduler:
    """Schedule a single callback, optionally waking the PC from sleep."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._timer: int | None = None
        self._cancel_event: int | None = None
        self._scheduled_for: datetime | None = None
        self._using_wake_timer = False

    @property
    def scheduled_for(self) -> datetime | None:
        with self._lock:
            return self._scheduled_for

    @property
    def using_wake_timer(self) -> bool:
        with self._lock:
            return self._using_wake_timer

    def schedule(self, when: datetime, callback: Callable[[], None]) -> bool:
        """Schedule *callback* for *when*.

        Returns True when a Windows wake timer was used, False when the
        fallback sleep was used.  Any previous schedule is cancelled.
        """

        self.cancel()
        seconds = max(0.0, (when - datetime.now()).total_seconds())
        # At least one tick in the future; a zero due time is an absolute date
        # in the distant past and may fire in surprising ways.
        seconds = max(0.001, seconds)

        timer = _CreateWaitableTimerExW(
            None,
            None,
            CREATE_WAITABLE_TIMER_MANUAL_RESET,
            TIMER_ALL_ACCESS,
        )
        cancel_event = _CreateEventW(None, True, False, None)
        if timer and cancel_event:
            due = ctypes.c_longlong(-max(1, int(seconds * 10_000_000)))
            ok = _SetWaitableTimer(timer, ctypes.byref(due), 0, None, None, True)
            used_wake = bool(ok)
            if not ok:
                err = ctypes.get_last_error()
                log.warning("SetWaitableTimer(fResume=True) failed (%s); retrying without wake", err)
                ok = _SetWaitableTimer(timer, ctypes.byref(due), 0, None, None, False)
                used_wake = False
            if ok:
                thread = threading.Thread(
                    target=self._wait_for_timer,
                    args=(timer, cancel_event, callback),
                    name="WakeTimer",
                    daemon=True,
                )
                with self._lock:
                    self._timer = timer
                    self._cancel_event = cancel_event
                    self._thread = thread
                    self._scheduled_for = when
                    self._using_wake_timer = used_wake
                thread.start()
                log.info(
                    "Wake timer set for %s (wake=%s, in %.0fs)",
                    when.strftime("%Y-%m-%d %H:%M:%S"),
                    used_wake,
                    seconds,
                )
                return used_wake
            _CloseHandle(timer)
        if cancel_event:
            _CloseHandle(cancel_event)

        # Fallback: a plain thread.  It can't wake the PC, but it still works
        # while the PC is awake and keeps the app functional.
        log.warning("Could not create a waitable timer; using a sleep fallback")
        cancel_event = _CreateEventW(None, True, False, None)
        if not cancel_event:
            return False
        thread = threading.Thread(
            target=self._wait_for_sleep,
            args=(seconds, cancel_event, callback),
            name="WakeTimerFallback",
            daemon=True,
        )
        with self._lock:
            self._cancel_event = cancel_event
            self._thread = thread
            self._scheduled_for = when
            self._using_wake_timer = False
        thread.start()
        return False

    def cancel(self) -> None:
        with self._lock:
            thread = self._thread
            cancel_event = self._cancel_event
            timer = self._timer
            self._thread = None
            self._cancel_event = None
            self._timer = None
            self._scheduled_for = None
            self._using_wake_timer = False

        if cancel_event:
            _SetEvent(cancel_event)
        if timer:
            _CancelWaitableTimer(timer)
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        if cancel_event:
            _CloseHandle(cancel_event)
        if timer:
            _CloseHandle(timer)

    def _wait_for_timer(
        self,
        timer: int,
        cancel_event: int,
        callback: Callable[[], None],
    ) -> None:
        handles = (wintypes.HANDLE * 2)(timer, cancel_event)
        try:
            result = _WaitForMultipleObjects(2, handles, False, INFINITE)
            if result == WAIT_OBJECT_0:
                try:
                    callback()
                except Exception:
                    log.exception("Wake timer callback failed")
        finally:
            # The scheduler owns the handles and closes them in cancel().
            pass

    def _wait_for_sleep(
        self,
        seconds: float,
        cancel_event: int,
        callback: Callable[[], None],
    ) -> None:
        # WaitForSingleObject on the event gives us a cancellable sleep.
        wait = _WaitForSingleObject(cancel_event, int(seconds * 1000))
        if wait == WAIT_TIMEOUT:
            try:
                callback()
            except Exception:
                log.exception("Fallback timer callback failed")


# The fallback uses WaitForSingleObject; define it separately for clarity.
_WaitForSingleObject = _k32.WaitForSingleObject
_WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_WaitForSingleObject.restype = wintypes.DWORD
