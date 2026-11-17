"""Windows UI Automation adapter for Claude Desktop.

The module is intentionally defensive: Claude Desktop is an Electron app and
its accessibility tree changes between releases.  Every lookup has a timeout
and a fallback where practical.
"""

from __future__ import annotations

import ctypes
import logging
import re
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

import uiautomation as auto

from .input_safety import (
    get_clipboard_text,
    paste_image,
    release_modifiers,
    set_clipboard_text,
)
from .process_info import window_belongs_to
from .rate_limit import LimitInfo, limit_info, limit_is_active, looks_like_limit

log = logging.getLogger(__name__)

auto.SetGlobalSearchTimeout(1.5)

WINDOW_CLASS = "Chrome_WidgetWin_1"
WINDOW_TITLE = "Claude"
COMPOSER_NAME = "Write your prompt to Claude"
MODAL_BUTTONS = ("Not now", "Explore plans", "Maybe later")
STOP_BUTTON_NAMES = (
    "stop",
    "stop response",
    "stop responding",
    "stop generating",
    "stop streaming",
)


def is_stop_button(name: str) -> bool:
    """Return True when *name* is the Stop button shown during a response."""

    lowered = re.sub(r"\s+", " ", (name or "")).strip().lower()
    if lowered in STOP_BUTTON_NAMES:
        return True
    return lowered.startswith(
        ("stop response", "stop responding", "stop generating", "stop streaming")
    )


def _near_composer_area(
    window_rect: tuple[int, int, int, int],
    control_rect: tuple[int, int, int, int],
) -> bool:
    """Stop buttons live in the lower-right area, next to the composer.

    Checking the position avoids mistaking a sidebar chat literally named
    "Stop" for the streaming control.
    """

    left, top, right, bottom = window_rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    cx = (control_rect[0] + control_rect[2]) // 2
    cy = (control_rect[1] + control_rect[3]) // 2
    return cx >= left + width * 0.5 and cy >= top + height * 0.5

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetForegroundWindow.argtypes = []
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.SetCursorPos.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.restype = wintypes.BOOL

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD

SW_RESTORE = 9
SW_MINIMIZE = 6

_EXCLUDED_SIDEBAR_NAMES = {
    "new",
    "search",
    "projects",
    "artifacts",
    "customize",
    "pinned",
    "recents",
    "view all",
    "filter and group recents",
    "new from a template",
    "get apps and extensions",
    "kutaos free",
    "upgrade",
}
_EXCLUDED_PREFIXES = (
    "toggle sessions for ",
    "more options for ",
)

_STRONG_BANNER_RE = re.compile(
    r"out of free messages|out of messages|free messages return|"
    r"you hit your .{0,40}limit|"
    r"you(?:'ve| have) hit (?:the|your) .{0,40}limit|"
    r"you(?:'ve| have) reached your .{0,30}limit|"
    r"limit for claude messages|"
    r"limits? will reset|"
    r"(?:please )?wait before trying again",
    re.IGNORECASE,
)

_OVERLAY_TOKENS = ("toast", "notification", "snackbar", "alert", "popover", "banner")
_FAILED_SEND_RE = re.compile(r"\bfailed to send\b", re.IGNORECASE)


@dataclass
class TextNode:
    name: str
    left: int
    top: int
    right: int
    bottom: int
    in_status_bar: bool = False
    in_overlay: bool = False

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)


@dataclass
class UiSnapshot:
    tree_ready: bool
    limit: LimitInfo | None
    limit_text: str
    modal_open: bool
    composer_present: bool
    chat_title: str
    window_rect: tuple[int, int, int, int]
    text_nodes: list[TextNode] = field(default_factory=list)
    button_rects: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    processing: bool = False


@dataclass
class FocusState:
    claude_hwnd: int
    previous_hwnd: int
    was_minimized: bool


@dataclass
class SendResult:
    ok: bool
    message: str
    rate_limited: bool = False
    # ``submitted`` means the Send button was invoked but the UI never gave a
    # confirmation.  The send must not be retried: a second click can land on
    # the Stop button and interrupt the response that is already streaming.
    submitted: bool = False
    # ``processing`` means a response was still streaming, so nothing was sent.
    processing: bool = False
    # A failed post-send submit can leave the message and images restored in the
    # composer.  Keep them instead of overwriting the restored draft.
    keep_composer: bool = False


# ---------------------------------------------------------------------------
# Window discovery / focus
# ---------------------------------------------------------------------------


def find_window() -> auto.Control | None:
    """Find the Claude Desktop top-level window."""

    candidates: list[tuple[int, auto.Control]] = []
    try:
        root = auto.GetRootControl()
        for window in root.GetChildren():
            try:
                if (window.ClassName or "") != WINDOW_CLASS:
                    continue
                title = (window.Name or "").strip().lower()
                if not title.startswith("claude"):
                    continue
                hwnd = int(window.NativeWindowHandle or 0)
                if not hwnd or not _user32.IsWindow(hwnd):
                    continue
                # Browsers use the same Chromium window class, and a tab can
                # easily be titled "Claude ...".  Only accept the desktop app.
                if not window_belongs_to(hwnd, "claude.exe"):
                    continue
                rect = get_window_rect(window)
                area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                if window.IsOffscreen:
                    area = max(1, area // 2)
                candidates.append((area, window))
            except Exception:
                continue
    except Exception:
        log.debug("Window enumeration failed", exc_info=True)
        return None

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def get_window_rect(window: auto.Control) -> tuple[int, int, int, int]:
    try:
        rect = window.BoundingRectangle
        if rect.right > rect.left and rect.bottom > rect.top:
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    hwnd = int(window.NativeWindowHandle or 0)
    if hwnd:
        try:
            r = wintypes.RECT()
            if _user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
    return (0, 0, 0, 0)


def has_tree(window: auto.Control) -> bool:
    """A quick check that Claude's web content is exposed through UIA."""

    try:
        if window.DocumentControl(searchDepth=30).Exists(0.12):
            return True
        if window.EditControl(Name=COMPOSER_NAME, searchDepth=30).Exists(0.12):
            return True
        if window.StatusBarControl(searchDepth=30).Exists(0.12):
            return True
    except Exception:
        pass
    return False


def warm_up(window: auto.Control, timeout: float = 3.0) -> bool:
    """Focus Claude briefly when a freshly started app exposes a blank tree."""

    if has_tree(window):
        return True
    state = focus_window(window)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if has_tree(window):
                return True
            time.sleep(0.15)
        return has_tree(window)
    finally:
        restore_focus(state)


def _force_foreground(hwnd: int) -> bool:
    """Raise *hwnd* even when Windows' foreground lock blocks SetForegroundWindow.

    Attaching to the current foreground thread is the standard way to satisfy
    the foreground lock; unlike a synthetic Alt keypress it cannot leave a
    modifier stuck down.
    """

    if not hwnd:
        return False
    foreground = int(_user32.GetForegroundWindow() or 0)
    if foreground == hwnd:
        return True

    foreground_tid = int(_user32.GetWindowThreadProcessId(foreground, None))
    current_tid = int(_kernel32.GetCurrentThreadId())
    attached = False
    if foreground_tid and foreground_tid != current_tid:
        attached = bool(
            _user32.AttachThreadInput(current_tid, foreground_tid, True)
        )
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
        # Works even when the window belongs to a different thread.
        try:
            auto.SetForegroundWindow(hwnd)
        except Exception:
            pass
    finally:
        if attached:
            _user32.AttachThreadInput(current_tid, foreground_tid, False)

    deadline = time.monotonic() + 0.8
    while time.monotonic() < deadline:
        if int(_user32.GetForegroundWindow() or 0) == hwnd:
            return True
        time.sleep(0.05)
    return int(_user32.GetForegroundWindow() or 0) == hwnd


def focus_window(window: auto.Control) -> FocusState:
    """Bring Claude to the foreground and return how to undo it."""

    hwnd = int(window.NativeWindowHandle or 0)
    previous = int(_user32.GetForegroundWindow() or 0)
    was_minimized = bool(_user32.IsIconic(hwnd))
    if was_minimized:
        _user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.15)

    _force_foreground(hwnd)
    time.sleep(0.12)

    if int(_user32.GetForegroundWindow() or 0) != hwnd:
        # Last resort: click inside the title bar.  Keep the click clear of
        # the top resize border and the window buttons.
        left, top, right, _bottom = get_window_rect(window)
        if right > left:
            point = _get_cursor()
            try:
                auto.Click(
                    left + max(60, min(300, (right - left) // 3)),
                    top + 20,
                )
            except Exception:
                log.debug("Title-bar click failed", exc_info=True)
            finally:
                _set_cursor(point)

    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        if int(_user32.GetForegroundWindow() or 0) == hwnd:
            break
        time.sleep(0.05)
    return FocusState(claude_hwnd=hwnd, previous_hwnd=previous, was_minimized=was_minimized)


def restore_focus(state: FocusState | None) -> None:
    if state is None:
        return
    try:
        if state.was_minimized and state.claude_hwnd:
            _user32.ShowWindow(state.claude_hwnd, SW_MINIMIZE)
        if (
            state.previous_hwnd
            and state.previous_hwnd != state.claude_hwnd
            and _user32.IsWindow(state.previous_hwnd)
        ):
            _force_foreground(state.previous_hwnd)
    except Exception:
        log.debug("Could not restore previous foreground window", exc_info=True)


def _get_cursor() -> wintypes.POINT:
    point = wintypes.POINT()
    try:
        _user32.GetCursorPos(ctypes.byref(point))
    except Exception:
        pass
    return point


def _set_cursor(point: wintypes.POINT) -> None:
    try:
        _user32.SetCursorPos(point.x, point.y)
    except Exception:
        pass


def is_foreground(window: auto.Control) -> bool:
    """True when *window* is the foreground window.

    Synthetic wheel/keys must never be sent while another app is in front.
    """

    try:
        hwnd = int(window.NativeWindowHandle or 0)
        return bool(hwnd) and int(_user32.GetForegroundWindow() or 0) == hwnd
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def scan_window(window: auto.Control, now: datetime | None = None) -> UiSnapshot:
    """Read the pieces of Claude's UI that matter for the automation."""

    now = now or datetime.now()
    rect = get_window_rect(window)
    text_nodes: list[TextNode] = []
    button_rects: dict[str, tuple[int, int, int, int]] = {}
    composer_present = False
    chat_title = ""
    doc_seen = False
    modal_from_button = False
    processing = False

    for control in _walk(window, max_depth=55, max_nodes=6000):
        try:
            control_type = control.ControlType
            name = (control.Name or "").strip()
            in_bar = False
            # Track status-bar ancestry explicitly below rather than relying on
            # a single traversal flag; this loop walks the whole tree.
            if control_type == auto.ControlType.StatusBarControl:
                in_bar = True
            if control_type == auto.ControlType.DocumentControl:
                doc_seen = True
                if name and not chat_title:
                    chat_title = re.sub(
                        r"\s*-\s*Claude\s*$", "", name, flags=re.IGNORECASE
                    )
            if control_type == auto.ControlType.EditControl and name == COMPOSER_NAME:
                composer_present = True
            if control_type in (
                auto.ControlType.ButtonControl,
                auto.ControlType.HyperlinkControl,
            ) and name:
                r = _control_rect(control)
                button_rects.setdefault(name, r)
                if name in MODAL_BUTTONS:
                    modal_from_button = True
                if (
                    is_stop_button(name)
                    and not _is_offscreen(control)
                    and _near_composer_area(rect, r)
                ):
                    processing = True
            if control_type == auto.ControlType.TextControl and name:
                r = _control_rect(control)
                text_nodes.append(
                    TextNode(
                        name=name,
                        left=r[0],
                        top=r[1],
                        right=r[2],
                        bottom=r[3],
                        in_status_bar=_inside_status_bar(control),
                        in_overlay=_inside_overlay(control),
                    )
                )
            elif (
                control_type
                in (
                    auto.ControlType.GroupControl,
                    auto.ControlType.PaneControl,
                    auto.ControlType.ToolTipControl,
                )
                and name
                and _inside_overlay(control)
            ):
                # A side pop / toast can expose its message on the container
                # while the visible text is split into several child spans.
                r = _control_rect(control)
                text_nodes.append(
                    TextNode(
                        name=name,
                        left=r[0],
                        top=r[1],
                        right=r[2],
                        bottom=r[3],
                        in_overlay=True,
                    )
                )
        except Exception:
            continue

    tree_ready = doc_seen or composer_present or any(n.in_status_bar for n in text_nodes)

    # A modal may be exposed as text without the usual buttons.
    modal_open = modal_from_button or any(
        "keep chatting with claude pro" in node.name.lower() for node in text_nodes
    )
    if not modal_open:
        modal_open = _control_exists(window, "Keep chatting with Claude Pro")

    limit_text = _choose_limit_text(text_nodes, modal_open, rect, button_rects)
    info = limit_info(limit_text, now) if limit_text else None

    return UiSnapshot(
        tree_ready=tree_ready,
        limit=info,
        limit_text=limit_text or "",
        modal_open=modal_open,
        composer_present=composer_present,
        chat_title=chat_title,
        window_rect=rect,
        text_nodes=text_nodes,
        button_rects=button_rects,
        processing=processing,
    )


def _is_offscreen(control: auto.Control) -> bool:
    try:
        return bool(control.IsOffscreen)
    except Exception:
        return False


def _processing(window: auto.Control) -> bool:
    """Return True while a response is streaming (the Stop button is visible)."""

    rect = get_window_rect(window)
    for control in _walk(window, max_depth=55, max_nodes=6000):
        try:
            if control.ControlType != auto.ControlType.ButtonControl:
                continue
            if (
                is_stop_button(control.Name)
                and not _is_offscreen(control)
                and _near_composer_area(rect, _control_rect(control))
            ):
                return True
        except Exception:
            continue
    return False


def _inside_status_bar(control: auto.Control) -> bool:
    current = control
    for _ in range(8):
        try:
            current = current.GetParentControl()
            if current is None:
                return False
            if current.ControlType == auto.ControlType.StatusBarControl:
                return True
        except Exception:
            return False
    return False


def _inside_overlay(control: auto.Control) -> bool:
    """True when *control* lives in a toast, alert, or similar side pop."""

    current = control
    for _ in range(10):
        try:
            if current.ControlType == auto.ControlType.ToolTipControl:
                return True
            kind = (current.ClassName or "").lower()
            automation_id = str(getattr(current, "AutomationId", "") or "").lower()
            marker = f"{kind} {automation_id}"
            if any(token in marker for token in _OVERLAY_TOKENS):
                return True
            current = current.GetParentControl()
            if current is None:
                return False
        except Exception:
            return False
    return False


def _control_rect(control: auto.Control) -> tuple[int, int, int, int]:
    try:
        r = control.BoundingRectangle
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return (0, 0, 0, 0)


def _choose_limit_text(
    nodes: list[TextNode],
    modal_open: bool,
    window_rect: tuple[int, int, int, int],
    button_rects: dict[str, tuple[int, int, int, int]],
) -> str | None:
    bar_texts = [node.name for node in nodes if node.in_status_bar]
    found = _best_limit_text(bar_texts)
    if found:
        return found

    # Developer mode can show the limit as a compact side pop instead of a
    # modal. Its message is often split into several spans, so join nearby text
    # before trusting a single node.
    grouped = _grouped_limit_text(nodes, window_rect)
    if grouped:
        return grouped

    # Toast/alert containers are safe to trust even without the exact banner
    # wording used by the standard modal.
    overlay_texts = [node.name for node in nodes if node.in_overlay and node.name]
    found = _best_limit_text(overlay_texts)
    if found:
        return found

    # Banners that are not in a status bar: only trust very specific phrases.
    strong = [
        node.name
        for node in nodes
        if not node.in_status_bar and _STRONG_BANNER_RE.search(node.name)
    ]
    found = _best_limit_text(strong)
    if found:
        return found

    if not modal_open:
        return None

    # Modal text is often split across several spans.  Join the text near the
    # "Not now" button (or near the center of the window) and search that.
    left, top, right, bottom = window_rect
    not_now = button_rects.get("Not now")
    if not_now:
        cx = (not_now[0] + not_now[2]) // 2
        cy = (not_now[1] + not_now[3]) // 2
        x0, x1 = cx - 500, cx + 500
        y0, y1 = cy - 320, cy + 120
    else:
        cx, cy = (left + right) // 2, (top + bottom) // 2
        x0, x1 = cx - 500, cx + 500
        y0, y1 = cy - 320, cy + 220

    nearby: list[str] = []
    for node in nodes:
        if node.in_status_bar or not node.name:
            continue
        nx, ny = node.center
        if x0 <= nx <= x1 and y0 <= ny <= y1:
            nearby.append(node.name)
    return _best_limit_text(nearby)


def _grouped_limit_text(
    nodes: list[TextNode],
    window_rect: tuple[int, int, int, int],
) -> str | None:
    """Join compact nearby text, for side pops split across several spans."""

    left, top, right, bottom = window_rect
    window_width = max(1, right - left)
    window_height = max(1, bottom - top)
    visible = [
        node
        for node in nodes
        if node.name
        and not node.in_status_bar
        and node.width > 0
        and node.height > 0
    ]
    if len(visible) < 2:
        return None

    groups: list[list[TextNode]] = []
    for node in sorted(visible, key=lambda item: (item.top, item.left)):
        placed = False
        for group in groups:
            x0 = min(item.left for item in group)
            y0 = min(item.top for item in group)
            x1 = max(item.right for item in group)
            y1 = max(item.bottom for item in group)
            if (
                node.top <= y1 + 36
                and node.bottom >= y0 - 10
                and node.left <= x1 + 90
                and node.right >= x0 - 90
            ):
                group.append(node)
                placed = True
                break
        if not placed:
            groups.append([node])

    best: str | None = None
    for group in groups:
        if len(group) < 2:
            continue
        x0 = min(item.left for item in group)
        y0 = min(item.top for item in group)
        x1 = max(item.right for item in group)
        y1 = max(item.bottom for item in group)
        # A pop is compact; a chat paragraph or a whole panel is not.
        if x1 - x0 > window_width * 0.72 or y1 - y0 > window_height * 0.32:
            continue
        if y1 - y0 > 280:
            continue
        if any(len(item.name) > 400 for item in group):
            continue

        texts: list[str] = []
        seen: set[str] = set()
        for item in sorted(group, key=lambda node: (node.top, node.left)):
            text = re.sub(r"\s+", " ", item.name).strip()
            if text and text not in seen:
                seen.add(text)
                texts.append(text)
        joined = " ".join(texts)
        if len(joined) > 1500:
            continue
        if not looks_like_limit(joined):
            continue
        if not _STRONG_BANNER_RE.search(joined):
            continue
        if best is None or len(joined) > len(best):
            best = joined
    return best


def _best_limit_text(texts: list[str]) -> str | None:
    unique: list[str] = []
    seen: set[str] = set()
    for text in texts:
        text = re.sub(r"\s+", " ", text).strip()
        if text and text not in seen:
            seen.add(text)
            unique.append(text)
    candidates = [text for text in unique if looks_like_limit(text)]
    if candidates:
        return max(candidates, key=len)
    joined = " ".join(unique)
    if joined and looks_like_limit(joined):
        return joined[:3000]
    return None


# ---------------------------------------------------------------------------
# Modal handling
# ---------------------------------------------------------------------------


def is_modal_open(window: auto.Control) -> bool:
    for name in MODAL_BUTTONS:
        try:
            if window.ButtonControl(Name=name, searchDepth=30).Exists(0.08):
                return True
        except Exception:
            continue
    try:
        return window.TextControl(
            Name="Keep chatting with Claude Pro", searchDepth=30
        ).Exists(0.08)
    except Exception:
        return False


def dismiss_modal(window: auto.Control, timeout: float = 3.0) -> bool:
    """Click "Not now" if Claude's upgrade modal is open."""

    if not is_modal_open(window):
        return True

    clicked = False
    for name in MODAL_BUTTONS:
        for control in _find_controls(window, name, limit=4):
            if _invoke_or_click(control):
                log.info("Dismissed Claude modal via %r", name)
                clicked = True
                break
        if clicked:
            break

    if not clicked:
        # Escape usually closes the modal.  Make sure Claude is focused first.
        try:
            window.SetFocus()
            auto.SendKeys("{Esc}", interval=0.1, waitTime=0.3)
        except Exception:
            pass
        finally:
            release_modifiers()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_modal_open(window):
            return True
        time.sleep(0.12)
    return not is_modal_open(window)


# ---------------------------------------------------------------------------
# Sidebar chat list
# ---------------------------------------------------------------------------


def list_sidebar_chats(window: auto.Control) -> list[str]:
    """Return the chat names currently visible in the sidebar."""

    # Claude exposes more than one group named "Sidebar" (one is a tiny
    # mode-switcher in the title bar).  Pick the largest one.
    sidebars: list[tuple[int, auto.Control]] = []
    for control in _walk(window, max_depth=45, max_nodes=5000):
        try:
            if (
                control.ControlType == auto.ControlType.GroupControl
                and (control.Name or "").strip() == "Sidebar"
            ):
                rect = _control_rect(control)
                area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                sidebars.append((area, control))
        except Exception:
            continue
    if not sidebars:
        return []
    sidebars.sort(key=lambda item: item[0], reverse=True)
    sidebar = sidebars[0][1]

    found: list[tuple[int, str]] = []
    for control in _walk(sidebar, max_depth=25, max_nodes=3000):
        try:
            if control.ControlType != auto.ControlType.ButtonControl:
                continue
            name = _chat_button_title(control)
            if not name or not _is_chat_button(control, name):
                continue
            rect = _control_rect(control)
            if rect[2] - rect[0] < 80 or rect[3] - rect[1] < 20:
                continue
            found.append((rect[1], name))
        except Exception:
            continue

    # Preserve visual order and remove duplicates.
    found.sort(key=lambda item: item[0])
    names: list[str] = []
    seen: set[str] = set()
    for _top, name in found:
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names[:40]


def _chat_button_title(control: auto.Control) -> str:
    """Return the chat title without the status text Claude appends.

    Claude's chat rows expose an accessible name that concatenates the title
    with live status, for example ``Loop pad music webapp Running Includes an
    artifact``.  The bare title is the first TextControl descendant; the
    wrapper elements vary between releases, so search a few levels down
    instead of only looking at direct children.
    """

    try:
        for node in _walk(control, max_depth=5, max_nodes=80):
            if node is control:
                continue
            if node.ControlType != auto.ControlType.TextControl:
                continue
            name = re.sub(r"\s+", " ", node.Name or "").strip()
            if name:
                return name
    except Exception:
        pass
    return re.sub(r"\s+", " ", control.Name or "").strip()


def _is_chat_button(control: auto.Control, name: str) -> bool:
    low = name.lower()
    if low in _EXCLUDED_SIDEBAR_NAMES:
        return False
    if any(low.startswith(prefix) for prefix in _EXCLUDED_PREFIXES):
        return False
    classes: list[str] = []
    current = control
    for _ in range(10):
        try:
            current = current.GetParentControl()
            if current is None:
                break
            classes.append((current.ClassName or "").lower())
        except Exception:
            break
    joined = " ".join(classes)
    if "df-drag-shiftable" not in joined:
        return False
    return True


# ---------------------------------------------------------------------------
# Sidebar chat selection
# ---------------------------------------------------------------------------


def _document_title(window: auto.Control) -> str:
    """Return Claude's current chat title from the document control."""

    for control in _walk(window, max_depth=40, max_nodes=6000):
        try:
            if control.ControlType != auto.ControlType.DocumentControl:
                continue
            name = (control.Name or "").strip()
            if not name:
                continue
            name = re.sub(r"\s*-\s*Claude\s*$", "", name, flags=re.IGNORECASE)
            if name and name.lower() != "claude":
                return name
        except Exception:
            continue
    return ""


def _same_chat(left: str, right: str) -> bool:
    a = re.sub(r"\s+", " ", left or "").strip().lower()
    b = re.sub(r"\s+", " ", right or "").strip().lower()
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def _find_chat_button(window: auto.Control, name: str) -> auto.Control | None:
    wanted = re.sub(r"\s+", " ", name).strip().lower()
    if not wanted:
        return None
    buttons: list[auto.Control] = []
    sidebar = _largest_sidebar(window)
    source = sidebar if sidebar is not None else window
    for control in _walk(source, max_depth=30, max_nodes=5000):
        try:
            if control.ControlType != auto.ControlType.ButtonControl:
                continue
            value = _chat_button_title(control)
            if not value or not _is_chat_button(control, value):
                continue
            buttons.append(control)
        except Exception:
            continue

    def normalized(control: auto.Control) -> str:
        return re.sub(r"\s+", " ", _chat_button_title(control)).strip().lower()

    exact = [b for b in buttons if normalized(b) == wanted]
    if exact:
        return exact[0]
    starts = [b for b in buttons if normalized(b).startswith(wanted)]
    if starts:
        return min(starts, key=lambda b: len(_chat_button_title(b)))
    contains = [b for b in buttons if wanted in normalized(b)]
    if contains:
        return min(contains, key=lambda b: len(_chat_button_title(b)))
    return None


def _largest_sidebar(window: auto.Control) -> auto.Control | None:
    sidebars: list[tuple[int, auto.Control]] = []
    for control in _walk(window, max_depth=45, max_nodes=5000):
        try:
            if (
                control.ControlType == auto.ControlType.GroupControl
                and (control.Name or "").strip() == "Sidebar"
            ):
                rect = _control_rect(control)
                area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                sidebars.append((area, control))
        except Exception:
            continue
    if not sidebars:
        return None
    sidebars.sort(key=lambda item: item[0], reverse=True)
    return sidebars[0][1]


def _sidebar_rect(window: auto.Control) -> tuple[int, int, int, int] | None:
    sidebar = _largest_sidebar(window)
    if sidebar is not None:
        return _control_rect(sidebar)
    window_rect = get_window_rect(window)
    if window_rect[2] <= window_rect[0]:
        return None
    width = window_rect[2] - window_rect[0]
    return (
        window_rect[0],
        window_rect[1] + 80,
        window_rect[0] + min(420, max(240, int(width * 0.3))),
        window_rect[3] - 40,
    )


def _scroll_sidebar(window: auto.Control, down: bool, pages: int) -> None:
    if not is_foreground(window):
        log.info("Skipped sidebar scrolling: Claude is not in front")
        return
    rect = _sidebar_rect(window)
    if rect is None:
        return
    point = _get_cursor()
    try:
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        _user32.SetCursorPos(cx, cy)
        for _ in range(max(1, pages)):
            if down:
                auto.WheelDown(wheelTimes=3, interval=0.05, waitTime=0.3)
            else:
                auto.WheelUp(wheelTimes=3, interval=0.05, waitTime=0.3)
            time.sleep(0.15)
    finally:
        _set_cursor(point)


def _locate_chat(
    window: auto.Control,
    chat_name: str,
    *,
    attempts: int = 3,
    delay: float = 0.5,
) -> auto.Control | None:
    """Find a sidebar chat, tolerating a sidebar that is still re-rendering.

    Claude briefly rebuilds the chat list after a chat is opened or after it
    regains focus.  A single scan can therefore miss a chat that is clearly
    visible a moment later.
    """

    for attempt in range(max(1, attempts)):
        button = _find_chat_button(window, chat_name)
        if button is not None:
            return button
        if attempt < attempts - 1:
            time.sleep(delay)
    return None


def select_chat(window: auto.Control, chat_name: str) -> tuple[bool, str]:
    """Open a chat by clicking it in Claude's sidebar.

    Names in the sidebar are reliable; the command-palette search was not.
    If the chat is outside the visible part of the list, the sidebar is scrolled
    with the wheel and searched again.  Nothing is typed into the app.
    """

    chat_name = (chat_name or "").strip()
    if not chat_name:
        return True, "Using the currently open chat"

    button = _locate_chat(window, chat_name)
    if button is None:
        for down, pages in ((True, 8), (False, 12), (True, 8)):
            _scroll_sidebar(window, down=down, pages=pages)
            button = _locate_chat(window, chat_name, attempts=2, delay=0.4)
            if button is not None:
                break
    if button is None:
        return False, f"Chat {chat_name!r} was not found in Claude's sidebar"

    matched = _chat_button_title(button)
    clicked = False
    for attempt in range(2):
        clicked = _invoke_or_click(button)
        if clicked:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                dismiss_modal(window, timeout=0.7)
                title = _document_title(window)
                if _same_chat(title, matched) or _same_chat(title, chat_name):
                    return True, f"Selected chat {matched!r}"
                time.sleep(0.2)

        # The click can land while the list is re-rendering, or the control can
        # go stale.  Re-scan once before reporting a failure.
        if attempt == 0:
            button = _locate_chat(window, chat_name)
            if button is None:
                break
            matched = _chat_button_title(button)

    if not clicked:
        return False, f"Could not open chat {matched!r}"
    return False, f"Clicked {matched!r}, but Claude did not open that chat"


# ---------------------------------------------------------------------------
# Composer / sending
# ---------------------------------------------------------------------------


def _find_composer(window: auto.Control) -> auto.Control | None:
    try:
        composer = window.EditControl(Name=COMPOSER_NAME, searchDepth=30)
        if composer.Exists(0.2):
            return composer
    except Exception:
        pass
    # Fallback for a renamed composer.
    for control in _walk(window, max_depth=40, max_nodes=4000):
        try:
            if control.ControlType != auto.ControlType.EditControl:
                continue
            cls = (control.ClassName or "").lower()
            if "prosemirror" in cls or "tiptap" in cls:
                return control
        except Exception:
            continue
    return None


def _composer_text(composer: auto.Control) -> str:
    try:
        raw = composer.GetValuePattern().Value or ""
    except Exception:
        return ""
    return raw.replace("\u200b", "").strip()


def _set_composer_text(composer: auto.Control, text: str) -> bool:
    """Set the composer through ValuePattern; no synthetic typing."""

    try:
        composer.GetValuePattern().SetValue(text)
        time.sleep(0.35)
        return _composer_text(composer) == text.strip()
    except Exception:
        log.debug("Claude composer SetValue failed", exc_info=True)
        return False


def send_message(
    window: auto.Control,
    message: str,
    attachments: tuple[str, ...] = (),
    preserve_draft: bool = True,
) -> SendResult:
    """Put *message* in Claude's composer and submit it.

    Text is written through UI Automation's ValuePattern and submitted with the
    Send button's InvokePattern.  That avoids synthetic typing, dropped
    characters, stuck modifiers, and most focus stealing.  The Send button is
    the authoritative rate-limit check; a disabled button means the limit is
    still active.
    """

    dismiss_modal(window, timeout=1.5)
    if _processing(window):
        return SendResult(
            False,
            "Claude is still responding; waiting instead of interrupting it",
            processing=True,
        )
    composer = _find_composer(window)
    if composer is None:
        if is_modal_open(window):
            return SendResult(False, "Claude's upgrade modal is open", rate_limited=True)
        return SendResult(False, "Could not find Claude's message composer")

    message = (message or "").strip() or "continue"
    draft = _composer_text(composer)
    baseline_messages, baseline_failures = _count_send_markers(window, message)
    saved_clipboard = get_clipboard_text() if attachments else None

    try:
        if not _set_composer_text(composer, ""):
            return SendResult(False, "Could not clear Claude's message composer")

        if attachments:
            # Claude restores a failed submission, images included.  Start from
            # a clean attachment tray so the configured images are not doubled.
            _clear_composer_attachments(window)
            composer = _find_composer(window) or composer
            if _count_remove_buttons(window):
                return SendResult(
                    False,
                    "Could not clear Claude's restored attachments",
                )

        attached, failed = _paste_attachments(window, composer, attachments)
        composer = _find_composer(window) or composer

        if failed:
            return SendResult(False, f"Could not attach {failed!r} in Claude")
        if attached:
            log.info("Attached %d image(s) in Claude", attached)

        if not _set_composer_text(composer, message):
            # A single retry after focusing, in case Electron replaced the
            # composer element while the attachment was being pasted.
            try:
                composer.SetFocus()
                time.sleep(0.2)
            except Exception:
                pass
            composer = _find_composer(window) or composer
            if not _set_composer_text(composer, message):
                return SendResult(False, "Could not write the message into Claude's composer")

        button = _find_send_button(window, composer)
        if button is None:
            return _blocked_result(window, "Could not find Claude's Send button")
        if not _wait_for_enabled(button, 2.5):
            return _blocked_result(
                window,
                "Claude's Send button is disabled; the rate limit is still active",
            )

        if not _invoke_or_click(button):
            return SendResult(False, "Could not click Claude's Send button")

        result = _verify_sent(
            window,
            composer,
            message,
            baseline_messages,
            12.0,
            baseline_failures,
        )
        if result is None:
            # The Send button was invoked exactly once.  Never click it again:
            # by now it has become the Stop button, and a second click would
            # cancel the response that is already streaming.
            result = SendResult(
                True,
                "Message submitted (confirmation not readable yet)",
                submitted=True,
            )

        _restore_draft(
            window,
            draft,
            preserve_draft and not result.ok and not result.keep_composer,
        )
        return result
    finally:
        if saved_clipboard:
            set_clipboard_text(saved_clipboard)


def _blocked_result(window: auto.Control, reason: str) -> SendResult:
    snapshot = scan_window(window)
    if snapshot.limit is not None and limit_is_active(snapshot.limit):
        return SendResult(
            False,
            f"{reason}; the rate limit is still active",
            rate_limited=True,
        )
    if is_modal_open(window):
        dismiss_modal(window, timeout=1.0)
        return SendResult(
            False,
            f"{reason}; the upgrade modal is open",
            rate_limited=True,
        )
    return SendResult(False, reason)


def _restore_draft(window: auto.Control, draft: str, preserve_draft: bool) -> None:
    if not preserve_draft or not draft:
        return
    try:
        composer = _find_composer(window)
        if composer is not None:
            _set_composer_text(composer, draft)
    except Exception:
        log.debug("Could not restore Claude's draft", exc_info=True)


def _find_send_button(
    window: auto.Control,
    composer: auto.Control | None = None,
) -> auto.Control | None:
    """Find Claude's "Send message" button near the composer."""

    candidates: list[auto.Control] = []
    try:
        direct = window.ButtonControl(Name="Send message", searchDepth=30)
        if direct.Exists(0.15):
            candidates.append(direct)
    except Exception:
        pass
    if not candidates:
        for control in _walk(window, max_depth=45, max_nodes=5000):
            try:
                if (
                    control.ControlType == auto.ControlType.ButtonControl
                    and (control.Name or "").strip() == "Send message"
                ):
                    candidates.append(control)
            except Exception:
                continue

    composer_rect = _control_rect(composer) if composer is not None else (0, 0, 0, 0)
    best: tuple[int, auto.Control] | None = None
    for candidate in candidates:
        rect = _control_rect(candidate)
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            continue
        distance = 0
        if composer_rect[2] > composer_rect[0]:
            distance = abs(
                ((rect[1] + rect[3]) // 2)
                - ((composer_rect[1] + composer_rect[3]) // 2)
            )
        if best is None or distance < best[0]:
            best = (distance, candidate)
    return best[1] if best is not None else None


def _wait_for_enabled(control: auto.Control, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if control.IsEnabled:
                return True
        except Exception:
            return False
        time.sleep(0.1)
    return False


def _paste_attachments(
    window: auto.Control,
    composer: auto.Control,
    attachments: tuple[str, ...],
) -> tuple[int, str | None]:
    """Paste images into Claude's composer.  Returns (count, failed_path)."""

    attached = 0
    for path in attachments:
        if not Path(path).is_file():
            return attached, path
        before = _count_remove_buttons(window)
        ok = paste_image(
            window,
            composer,
            path,
            lambda: _count_remove_buttons(window) > before,
        )
        if not ok:
            return attached, path
        attached += 1
        composer = _find_composer(window) or composer
        time.sleep(0.2)
    return attached, None


def _composer_attachment_buttons(window: auto.Control) -> list[auto.Control]:
    buttons: list[auto.Control] = []
    for control in _walk(window, max_depth=55, max_nodes=8000):
        try:
            if control.ControlType != auto.ControlType.ButtonControl:
                continue
            name = (control.Name or "").strip().lower()
            if not name.startswith("remove "):
                continue
            if any(
                token in name
                for token in (
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".webp",
                    ".gif",
                    "image",
                    "photo",
                    "file",
                )
            ):
                buttons.append(control)
        except Exception:
            continue
    return buttons


def _count_remove_buttons(window: auto.Control) -> int:
    return len(_composer_attachment_buttons(window))


def _clear_composer_attachments(window: auto.Control) -> int:
    """Remove restored/failed attachments before pasting the configured set."""

    removed = 0
    for _ in range(20):
        buttons = _composer_attachment_buttons(window)
        if not buttons:
            break
        if not _invoke_or_click(buttons[0]):
            break
        removed += 1
        time.sleep(0.15)
    if removed:
        log.info("Removed %d existing attachment(s) in Claude", removed)
    return removed


def _verify_sent(
    window: auto.Control,
    composer: auto.Control,
    message: str,
    baseline_messages: int,
    timeout: float,
    baseline_failures: int = 0,
) -> SendResult | None:
    """Return a result when the send is confirmed or blocked, else None."""

    deadline = time.monotonic() + timeout
    next_limit_check = 0.0
    while time.monotonic() < deadline:
        if is_modal_open(window):
            dismiss_modal(window, timeout=1.0)
            return SendResult(
                False,
                "Claude showed the rate-limit/upgrade modal",
                rate_limited=True,
            )

        sent_count, failed_count = _count_send_markers(window, message)
        if failed_count > baseline_failures:
            return _post_send_failure(
                window,
                "Claude reported that the message failed to send",
            )
        if sent_count > baseline_messages:
            return _settle_after_send(
                window,
                "the message appeared in the chat",
                baseline_failures,
            )
        if _processing(window):
            return _settle_after_send(
                window,
                "Claude started responding",
                baseline_failures,
            )

        current = _find_composer(window)
        text = _composer_text(current) if current is not None else ""
        if text:
            # Developer mode can leave the failed message (and images) in the
            # composer while showing the limit only in a side pop.  Poll for
            # that pop without declaring the restored draft a successful send.
            now = time.monotonic()
            if now >= next_limit_check:
                snapshot = scan_window(window)
                if (
                    snapshot.limit is not None
                    and limit_is_active(snapshot.limit)
                ):
                    return SendResult(
                        False,
                        "Claude showed its rate limit after the send attempt",
                        rate_limited=True,
                        keep_composer=True,
                    )
                next_limit_check = now + 1.2
            time.sleep(0.3)
            continue

        # Give the submitted message a moment to render, then require either
        # the message itself or an empty composer with a disabled Send button.
        time.sleep(0.8)
        sent_count, failed_count = _count_send_markers(window, message)
        if failed_count > baseline_failures:
            return _post_send_failure(
                window,
                "Claude reported that the message failed to send",
            )
        if sent_count > baseline_messages:
            return _settle_after_send(
                window,
                "the message appeared in the chat",
                baseline_failures,
            )
        if _processing(window):
            return _settle_after_send(
                window,
                "Claude started responding",
                baseline_failures,
            )
        snapshot = scan_window(window)
        if snapshot.limit is not None and limit_is_active(snapshot.limit):
            return SendResult(
                False,
                "Claude is still showing its rate limit",
                rate_limited=True,
            )
        if is_modal_open(window):
            dismiss_modal(window, timeout=1.0)
            return SendResult(
                False,
                "Claude is showing a blocking dialog",
                rate_limited=True,
            )
        send_button = _find_send_button(window, current)
        if send_button is not None and not _control_enabled(send_button):
            return _settle_after_send(
                window,
                "the composer cleared",
                baseline_failures,
            )
        time.sleep(0.4)
    return None


def _control_enabled(control: auto.Control) -> bool:
    try:
        return bool(control.IsEnabled)
    except Exception:
        return False


def _count_send_markers(
    window: auto.Control,
    message: str,
) -> tuple[int, int]:
    """Count sent-message and failed-send markers in one accessibility walk."""

    wanted = re.sub(r"\s+", " ", message or "").strip().lower()
    label = f"you said: {wanted}"
    # Claude truncates long user messages in the accessibility tree at the
    # first line break, so match that prefix too.
    lines = (message or "").splitlines()
    first_line = re.sub(r"\s+", " ", lines[0] if lines else "").strip().lower()
    prefix = first_line[:80]
    sent = 0
    failed = 0
    for control in _walk(window, max_depth=55, max_nodes=6000):
        try:
            name = re.sub(r"\s+", " ", (control.Name or "")).strip().lower()
            if name and _FAILED_SEND_RE.search(name):
                failed += 1
            if control.ControlType != auto.ControlType.TextControl:
                continue
            if not wanted or not name:
                continue
            if name == label or (
                name.startswith("you said:")
                and (wanted in name or (prefix and prefix in name))
            ):
                sent += 1
        except Exception:
            continue
    return sent, failed


def _count_sent_messages(window: auto.Control, message: str) -> int:
    """Count visible copies of the user's message in the chat.

    Claude renders a submitted user message as ``You said: <message>`` (and
    usually duplicates that text through nested accessibility nodes).  A
    relative count before/after submitting tells us whether the message was
    actually sent even when the composer lags behind.
    """

    return _count_send_markers(window, message)[0]


def _count_failed_sends(window: auto.Control) -> int:
    return _count_send_markers(window, "")[1]


def _post_send_failure(window: auto.Control, reason: str) -> SendResult:
    """Handle a restored draft after Claude rejected the submitted message.

    Developer mode can show the limit as a side pop only after Send is invoked.
    The side pop can take a moment to render, so check for it briefly while
    leaving the restored message and images in the composer.
    """

    for delay in (0.0, 0.8, 1.4):
        if delay:
            time.sleep(delay)
        snapshot = scan_window(window)
        if snapshot.limit is not None and limit_is_active(snapshot.limit):
            return SendResult(
                False,
                f"{reason}; Claude's rate limit appeared after sending",
                rate_limited=True,
                keep_composer=True,
            )
    return SendResult(
        False,
        f"{reason}; Claude restored the message and images",
        keep_composer=True,
    )


def _settle_after_send(
    window: auto.Control,
    reason: str,
    baseline_failures: int | None = None,
) -> SendResult:
    """Wait briefly for a blocking modal before declaring success.

    A limit banner that appears here belongs to the *next* episode: the send
    was already confirmed, so it must not be reported as a failed send unless
    Claude also exposes a new ``Failed to send`` marker.
    """

    time.sleep(0.7)
    for _ in range(2):
        if (
            baseline_failures is not None
            and _count_failed_sends(window) > baseline_failures
        ):
            return _post_send_failure(
                window,
                "Claude reported that the message failed to send",
            )
        if is_modal_open(window):
            dismiss_modal(window, timeout=1.0)
            return SendResult(
                False,
                "Claude showed the rate-limit/upgrade modal",
                rate_limited=True,
            )
        time.sleep(0.3)
    if (
        baseline_failures is not None
        and _count_failed_sends(window) > baseline_failures
    ):
        return _post_send_failure(
            window,
            "Claude reported that the message failed to send",
        )
    return SendResult(True, f"Message sent ({reason})")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _walk(
    control: auto.Control,
    max_depth: int = 50,
    max_nodes: int = 5000,
) -> Iterator[auto.Control]:
    stack: list[tuple[auto.Control, int]] = [(control, 0)]
    count = 0
    while stack:
        current, depth = stack.pop()
        yield current
        count += 1
        if count >= max_nodes or depth >= max_depth:
            continue
        try:
            children = current.GetChildren()
        except Exception:
            children = []
        for child in reversed(children):
            stack.append((child, depth + 1))


def _find_controls(
    window: auto.Control,
    name: str,
    limit: int = 8,
) -> list[auto.Control]:
    found: list[auto.Control] = []
    for control in _walk(window, max_depth=45, max_nodes=5000):
        try:
            if (control.Name or "").strip() == name:
                found.append(control)
                if len(found) >= limit:
                    break
        except Exception:
            continue
    return found


def _control_exists(window: auto.Control, name: str) -> bool:
    try:
        return window.Control(Name=name, searchDepth=30).Exists(0.12)
    except Exception:
        return False


def _invoke_or_click(control: auto.Control) -> bool:
    try:
        control.GetInvokePattern().Invoke()
        return True
    except Exception:
        pass
    point = _get_cursor()
    try:
        control.Click()
        return True
    except Exception:
        log.debug("Could not click control %r", getattr(control, "Name", "?"), exc_info=True)
        return False
    finally:
        _set_cursor(point)
