"""UI Automation adapter for the ChatGPT / Codex desktop app.

The app is Electron-based and exposes a Chrome_WidgetWin_1 window.  Its
accessibility tree is only populated after the window has been focused once.
The adapter deliberately uses UI Automation patterns (SetValue / Invoke) instead
of synthetic typing wherever possible; that keeps the app from stealing the
keyboard and avoids stuck modifier keys.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path

import uiautomation as auto

from .claude_uia import (
    SendResult,
    TextNode,
    UiSnapshot,
    _best_limit_text,
    _control_rect,
    _get_cursor,
    _invoke_or_click,
    _near_composer_area,
    _set_cursor,
    _walk,
    focus_window,
    get_window_rect,
    is_foreground,
    is_stop_button,
    restore_focus,
)
from .input_safety import (
    get_clipboard_text,
    paste_image,
    release_modifiers,
    set_clipboard_text,
)
from .process_info import window_belongs_to
from .rate_limit import limit_info, limit_is_active, looks_like_limit

log = logging.getLogger(__name__)

auto.SetGlobalSearchTimeout(1.5)

WINDOW_CLASS = "Chrome_WidgetWin_1"
WINDOW_TITLE = "chatgpt"
COMPOSER_NAMES = ("Ask ChatGPT", "Message ChatGPT", "Message")
PLACEHOLDER_TEXTS = {"ask chatgpt", "message chatgpt", "message"}
SEND_BUTTON_NAMES = ("Send", "Send message")
MODAL_BUTTONS = ("Not now", "Maybe later")
PREFERENCE_TEXT = "Which option do you prefer?"

_EXCLUDED_SIDEBAR_NAMES = {
    "back",
    "forward",
    "hide sidebar",
    "minimize",
    "maximize",
    "restore",
    "close",
    "home",
    "images",
    "scheduled",
    "library",
    "plugins",
    "explore",
    "open profile menu",
    "new chat",
    "projects",
    "project sidebar options",
    "add new project",
    "recents",
    "chat sidebar options",
    "filter chats and work",
    "free offer",
    "temporary chat",
    "chat",
    "work",
    "new tab",
    "learn something",
    "dismiss suggestion",
    "add files and more",
    "select chatgpt model",
    "dictate",
    "send",
    "switch mode, current mode: chatgpt",
    "view activity",
    "search",
    "app navigation",
    "chat toolbar",
    "composer mode",
}
_EXCLUDED_PREFIXES = (
    "project actions for ",
    "new chat in ",
    "start new chat in ",
    "chat actions",
    "pin chat",
    "remove image",
    "remove file",
    "more options for ",
)
_TOOLBAR_EXCLUDED = {
    "free offer",
    "chat actions",
    "dismiss upgrade reminder",
    "share",
    "new tab",
    "temporary chat",
    "chat",
    "work",
    "switch mode, current mode: chatgpt",
    "view activity",
    "search",
    "new chat",
    "minimize",
    "maximize",
    "restore",
    "close",
}


# ---------------------------------------------------------------------------
# Window discovery / focus
# ---------------------------------------------------------------------------


def find_window() -> auto.Control | None:
    candidates: list[tuple[int, auto.Control]] = []
    try:
        root = auto.GetRootControl()
        for window in root.GetChildren():
            try:
                if (window.ClassName or "") != WINDOW_CLASS:
                    continue
                title = (window.Name or "").strip().lower()
                if WINDOW_TITLE not in title:
                    continue
                hwnd = int(window.NativeWindowHandle or 0)
                if not hwnd:
                    continue
                # A browser tab titled "ChatGPT ..." shares the Chromium
                # window class; only accept the desktop app's own process.
                if not window_belongs_to(hwnd, "chatgpt.exe"):
                    continue
                rect = get_window_rect(window)
                area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                if window.IsOffscreen:
                    area = max(1, area // 2)
                candidates.append((area, window))
            except Exception:
                continue
    except Exception:
        log.debug("ChatGPT window enumeration failed", exc_info=True)
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def has_tree(window: auto.Control) -> bool:
    try:
        if window.EditControl(Name=COMPOSER_NAMES[0], searchDepth=40).Exists(0.15):
            return True
        if window.DocumentControl(searchDepth=40).Exists(0.1):
            return True
        if window.GroupControl(Name="Conversation", searchDepth=40).Exists(0.1):
            return True
    except Exception:
        pass
    for control in _walk(window, max_depth=40, max_nodes=3000):
        try:
            if control.ControlType == auto.ControlType.EditControl:
                cls = (control.ClassName or "").lower()
                if "prosemirror" in cls:
                    return True
        except Exception:
            continue
    return False


def warm_up(window: auto.Control, timeout: float = 3.0) -> bool:
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


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def scan_window(window: auto.Control, now: datetime | None = None) -> UiSnapshot:
    now = now or datetime.now()
    rect = get_window_rect(window)
    text_nodes: list[TextNode] = []
    button_rects: dict[str, tuple[int, int, int, int]] = {}
    composer_present = False
    doc_seen = False
    processing = False

    for control in _walk(window, max_depth=60, max_nodes=8000):
        try:
            control_type = control.ControlType
            name = (control.Name or "").strip()
            if control_type == auto.ControlType.DocumentControl:
                doc_seen = True
            if control_type == auto.ControlType.EditControl:
                cls = (control.ClassName or "").lower()
                if name in COMPOSER_NAMES or "prosemirror" in cls:
                    composer_present = True
            if control_type in (
                auto.ControlType.ButtonControl,
                auto.ControlType.HyperlinkControl,
            ) and name:
                r = _control_rect(control)
                button_rects.setdefault(name, r)
                if control_type == auto.ControlType.ButtonControl:
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
                        in_status_bar=False,
                    )
                )
        except Exception:
            continue

    chat_title = _toolbar_title(window)
    modal_open = _modal_open_from_buttons(button_rects)
    limit_text = _choose_limit_text(text_nodes, rect, button_rects, modal_open)
    info = limit_info(limit_text, now) if limit_text else None

    return UiSnapshot(
        tree_ready=doc_seen or composer_present,
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
    for control in _walk(window, max_depth=60, max_nodes=8000):
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


def _toolbar_title(window: auto.Control) -> str:
    """Return the chat title shown at the top of ChatGPT, or "" for a new chat."""

    window_rect = get_window_rect(window)
    if window_rect[2] <= window_rect[0]:
        return ""

    # The document control carries the chat title in current builds.  Ignore
    # the embedded sandbox documents and the generic window title.
    ignored = {"", "chatgpt", "sandbox", "document"}
    for control in _walk(window, max_depth=40, max_nodes=6000):
        try:
            if control.ControlType != auto.ControlType.DocumentControl:
                continue
            name = re.sub(r"\s*-\s*ChatGPT\s*$", "", (control.Name or "").strip(), flags=re.IGNORECASE)
            if name.lower() not in ignored:
                return name
        except Exception:
            continue

    # Older builds expose the title as a toolbar button.  Locate it by its
    # position relative to the window so the code is independent of scaling.
    left, top, right, bottom = window_rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    x0 = left + int(width * 0.28)
    x1 = left + int(width * 0.88)
    y0 = top + 28
    y1 = top + min(130, int(height * 0.18))
    candidates: list[tuple[int, str]] = []
    for control in _walk(window, max_depth=55, max_nodes=10000):
        try:
            if control.ControlType != auto.ControlType.ButtonControl:
                continue
            name = (control.Name or "").strip()
            if not name or name.lower() in _TOOLBAR_EXCLUDED:
                continue
            rect = _control_rect(control)
            if rect[2] - rect[0] < 60 or rect[3] - rect[1] < 16:
                continue
            if not (x0 <= rect[0] <= x1 and y0 <= rect[1] <= y1):
                continue
            candidates.append((rect[0], name))
        except Exception:
            continue
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _modal_open_from_buttons(
    button_rects: dict[str, tuple[int, int, int, int]],
) -> bool:
    for name in MODAL_BUTTONS:
        if name in button_rects:
            return True
    return False


def _choose_limit_text(
    nodes: list[TextNode],
    window_rect: tuple[int, int, int, int],
    button_rects: dict[str, tuple[int, int, int, int]],
    modal_open: bool,
) -> str | None:
    left, top, right, bottom = window_rect
    height = max(1, bottom - top)
    bottom_y = top + int(height * 0.45)

    bottom_texts = [node.name for node in nodes if node.top >= bottom_y]
    found = _best_limit_text([text for text in bottom_texts if looks_like_limit(text)])
    if found:
        return found

    if modal_open:
        cx = (left + right) // 2
        cy = (top + bottom) // 2
        nearby = [
            node.name
            for node in nodes
            if abs(node.center[0] - cx) <= 650 and abs(node.center[1] - cy) <= 420
        ]
        found = _best_limit_text([text for text in nearby if looks_like_limit(text)])
        if found:
            return found

    upgrade = (
        button_rects.get("Upgrade")
        or button_rects.get("Upgrade plan")
        or button_rects.get("Upgrade to Plus")
        or button_rects.get("Get Plus")
        or button_rects.get("See plans")
    )
    if upgrade:
        ux = (upgrade[0] + upgrade[2]) // 2
        uy = (upgrade[1] + upgrade[3]) // 2
        nearby = [
            node.name
            for node in nodes
            if abs(node.center[0] - ux) <= 750 and abs(node.center[1] - uy) <= 450
        ]
        found = _best_limit_text([text for text in nearby if looks_like_limit(text)])
        if found:
            return found

    return None


# ---------------------------------------------------------------------------
# Sidebar chats
# ---------------------------------------------------------------------------


def list_sidebar_chats(window: auto.Control) -> list[str]:
    try:
        window_rect = get_window_rect(window)
        left_limit = window_rect[0] + 500
        found: list[tuple[int, str]] = []
        for control in _walk(window, max_depth=60, max_nodes=10000):
            try:
                if control.ControlType != auto.ControlType.ButtonControl:
                    continue
                name = (control.Name or "").strip()
                if not name:
                    continue
                rect = _control_rect(control)
                if rect[2] - rect[0] < 120 or not (20 <= rect[3] - rect[1] <= 70):
                    continue
                if rect[0] > left_limit:
                    continue
                if rect[1] < window_rect[1] + 300:
                    continue
                if not _is_chat_button(control, name):
                    continue
                found.append((rect[1], name))
            except Exception:
                continue
    except Exception:
        log.debug("Could not list ChatGPT sidebar chats", exc_info=True)
        return []

    found.sort(key=lambda item: item[0])
    names: list[str] = []
    seen: set[str] = set()
    for _top, name in found:
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names[:60]


def _is_chat_button(control: auto.Control, name: str) -> bool:
    low = name.lower()
    if low in _EXCLUDED_SIDEBAR_NAMES:
        return False
    if any(low.startswith(prefix) for prefix in _EXCLUDED_PREFIXES):
        return False
    if low in ("new chat", "recents", "projects", "chat actions", "pin chat"):
        return False

    parent = _nearest_list_item(control)
    if parent is not None:
        actions = _descendant_names(parent)
        if any(
            action.lower().startswith(("chat actions", "pin chat"))
            for action in actions
        ):
            return True

    # Chats inside an expanded project live under a list named "Chats in ...".
    current = control
    for _ in range(5):
        try:
            current = current.GetParentControl()
            if current is None:
                break
            if (
                current.ControlType == auto.ControlType.ListControl
                and (current.Name or "").strip().lower().startswith("chats in ")
            ):
                return True
            if (
                current.ControlType == auto.ControlType.ListItemControl
                and (current.Name or "").strip().lower() == low
                and any(
                    action.lower().startswith("project actions")
                    for action in _descendant_names(current)
                )
            ):
                # This is the project row itself, not a chat.
                return False
        except Exception:
            break
    return False


def _nearest_list_item(control: auto.Control) -> auto.Control | None:
    current = control
    for _ in range(5):
        try:
            current = current.GetParentControl()
            if current is None:
                return None
            if current.ControlType == auto.ControlType.ListItemControl:
                return current
        except Exception:
            return None
    return None


def _descendant_names(control: auto.Control) -> list[str]:
    names: list[str] = []
    for child in _walk(control, max_depth=6, max_nodes=200):
        try:
            if child is control:
                continue
            name = (child.Name or "").strip()
            if name:
                names.append(name)
        except Exception:
            continue
    return names


# ---------------------------------------------------------------------------
# Chat selection
# ---------------------------------------------------------------------------


def select_chat(window: auto.Control, chat_name: str) -> tuple[bool, str]:
    chat_name = (chat_name or "").strip()
    if not chat_name:
        return True, "Using the currently open chat"

    button = _find_chat_button(window, chat_name)
    if button is None:
        _scroll_sidebar(window, down=True, pages=8)
        button = _find_chat_button(window, chat_name)
    if button is None:
        _scroll_sidebar(window, down=False, pages=12)
        button = _find_chat_button(window, chat_name)
    if button is None:
        _scroll_sidebar(window, down=True, pages=8)
        button = _find_chat_button(window, chat_name)
    if button is None:
        return False, f"Chat {chat_name!r} was not found in the sidebar"

    matched_name = (button.Name or "").strip()
    if not _invoke_or_click(button):
        return False, f"Could not click chat {matched_name!r}"

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        dismiss_modal(window)
        title = _toolbar_title(window)
        if title and _same_chat(title, matched_name):
            return True, f"Selected chat {matched_name!r}"
        if title and _same_chat(title, chat_name):
            return True, f"Selected chat {title!r}"
        time.sleep(0.2)

    return False, f"Clicked {matched_name!r}, but ChatGPT did not open that chat"


def _same_chat(a: str, b: str) -> bool:
    left = re.sub(r"\s+", " ", a or "").strip().lower()
    right = re.sub(r"\s+", " ", b or "").strip().lower()
    return bool(left and right) and (
        left == right or left.startswith(right) or right.startswith(left)
    )


def _find_chat_button(window: auto.Control, name: str) -> auto.Control | None:
    wanted = re.sub(r"\s+", " ", name).strip().lower()
    exact: list[auto.Control] = []
    starts: list[auto.Control] = []
    contains: list[auto.Control] = []
    sidebar = _sidebar_control(window)
    source = sidebar if sidebar is not None else window
    for control in _walk(source, max_depth=45, max_nodes=8000):
        try:
            if control.ControlType != auto.ControlType.ButtonControl:
                continue
            value = re.sub(r"\s+", " ", (control.Name or "")).strip().lower()
            if not value or not _is_chat_button(control, control.Name or ""):
                continue
            if value == wanted:
                exact.append(control)
            elif value.startswith(wanted):
                starts.append(control)
            elif wanted in value:
                contains.append(control)
        except Exception:
            continue
    if exact:
        return exact[0]
    if starts:
        return min(starts, key=lambda c: len(c.Name or ""))
    if contains:
        return min(contains, key=lambda c: len(c.Name or ""))
    return None


def _scroll_sidebar(window: auto.Control, down: bool, pages: int) -> None:
    if not is_foreground(window):
        log.info("Skipped sidebar scrolling: ChatGPT is not in front")
        return
    rect = _sidebar_rect(window)
    if rect is None:
        return
    point = _get_cursor()
    try:
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        _set_cursor(type(point)(cx, cy))
        for _ in range(max(1, pages)):
            if down:
                auto.WheelDown(wheelTimes=3, interval=0.05, waitTime=0.3)
            else:
                auto.WheelUp(wheelTimes=3, interval=0.05, waitTime=0.3)
            time.sleep(0.15)
    finally:
        _set_cursor(point)


def _sidebar_control(window: auto.Control) -> auto.Control | None:
    window_rect = get_window_rect(window)
    width = max(1, window_rect[2] - window_rect[0])
    height = max(1, window_rect[3] - window_rect[1])
    left_edge = window_rect[0] + int(width * 0.38)
    right_edge = window_rect[0] + int(width * 0.58)
    best: tuple[int, auto.Control] | None = None
    fallback: tuple[int, auto.Control] | None = None
    for control in _walk(window, max_depth=45, max_nodes=6000):
        try:
            if control.ControlType != auto.ControlType.GroupControl:
                continue
            rect = _control_rect(control)
            rw = rect[2] - rect[0]
            rh = rect[3] - rect[1]
            if not (180 <= rw <= 600 and rh >= max(280, int(height * 0.3))):
                continue
            if rect[0] < window_rect[0] - 20 or rect[2] > right_edge:
                continue
            if rect[0] > left_edge:
                continue
            area = rw * rh
            name = (control.Name or "").strip().lower()
            cls = (control.ClassName or "").lower()
            if name in ("home", "chats", "recents", "sidebar") or "navigation" in cls:
                if best is None or area > best[0]:
                    best = (area, control)
            elif fallback is None or area > fallback[0]:
                fallback = (area, control)
        except Exception:
            continue
    chosen = best or fallback
    return chosen[1] if chosen is not None else None


def _sidebar_rect(window: auto.Control) -> tuple[int, int, int, int] | None:
    control = _sidebar_control(window)
    if control is None:
        return None
    return _control_rect(control)


# ---------------------------------------------------------------------------
# Modal handling
# ---------------------------------------------------------------------------


def is_modal_open(window: auto.Control) -> bool:
    if _preference_overlay_open(window):
        return True
    for name in MODAL_BUTTONS:
        try:
            if window.ButtonControl(Name=name, searchDepth=40).Exists(0.08):
                return True
        except Exception:
            continue
    return False


def _preference_overlay_open(window: auto.Control) -> bool:
    try:
        return window.TextControl(Name=PREFERENCE_TEXT, searchDepth=30).Exists(0.12)
    except Exception:
        return False


def dismiss_modal(window: auto.Control, timeout: float = 2.0) -> bool:
    if _preference_overlay_open(window):
        _dismiss_preference_overlay(window)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _preference_overlay_open(window):
                return True
            time.sleep(0.1)
        return not _preference_overlay_open(window)

    if not is_modal_open(window):
        return True
    clicked = False
    for name in MODAL_BUTTONS:
        for control in _find_buttons(window, name):
            if _invoke_or_click(control):
                log.info("Dismissed ChatGPT modal via %r", name)
                clicked = True
                break
        if clicked:
            break
    if not clicked:
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
        time.sleep(0.1)
    return not is_modal_open(window)


def _dismiss_preference_overlay(window: auto.Control) -> None:
    """Close the one-time "Which option do you prefer?" survey.

    The survey is harmless but covers the whole window, so a send cannot be
    confirmed while it is open.  Esc dismisses it on current builds; clicking
    its Skip button is the fallback.
    """

    try:
        window.SetFocus()
        auto.SendKeys("{Esc}", interval=0.1, waitTime=0.4)
    except Exception:
        pass
    finally:
        release_modifiers()
    if not _preference_overlay_open(window):
        return
    for control in _find_buttons(window, "Skip"):
        if _invoke_or_click(control):
            log.info("Dismissed ChatGPT preference survey via Skip")
            time.sleep(0.4)
            return


def _find_buttons(window: auto.Control, name: str) -> list[auto.Control]:
    found: list[auto.Control] = []
    for control in _walk(window, max_depth=45, max_nodes=6000):
        try:
            if (
                control.ControlType == auto.ControlType.ButtonControl
                and (control.Name or "").strip() == name
            ):
                found.append(control)
                if len(found) >= 6:
                    break
        except Exception:
            continue
    return found


# ---------------------------------------------------------------------------
# Composer / sending
# ---------------------------------------------------------------------------


def _find_composer(window: auto.Control) -> auto.Control | None:
    for name in COMPOSER_NAMES:
        try:
            composer = window.EditControl(Name=name, searchDepth=40)
            if composer.Exists(0.15):
                return composer
        except Exception:
            pass
    candidates: list[tuple[int, auto.Control]] = []
    for control in _walk(window, max_depth=50, max_nodes=6000):
        try:
            if control.ControlType != auto.ControlType.EditControl:
                continue
            cls = (control.ClassName or "").lower()
            if "prosemirror" in cls:
                rect = _control_rect(control)
                candidates.append((rect[1], control))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _composer_text(composer: auto.Control) -> str:
    try:
        raw = composer.GetValuePattern().Value or ""
    except Exception:
        return ""
    text = raw.replace("\u200b", "").strip()
    if text.lower() in PLACEHOLDER_TEXTS:
        return ""
    return text


def _set_composer_text(composer: auto.Control, text: str) -> bool:
    try:
        vp = composer.GetValuePattern()
        vp.SetValue(text)
        time.sleep(0.35)
        return _composer_text(composer) == text.strip()
    except Exception:
        log.debug("ChatGPT composer SetValue failed", exc_info=True)
        return False


def send_message(
    window: auto.Control,
    message: str,
    attachments: tuple[str, ...] = (),
    preserve_draft: bool = True,
) -> SendResult:
    """Put *message* in ChatGPT's composer and submit it safely."""

    dismiss_modal(window, timeout=1.5)
    if _processing(window):
        return SendResult(
            False,
            "ChatGPT is still responding; waiting instead of interrupting it",
            processing=True,
        )
    composer = _find_composer(window)
    if composer is None:
        if is_modal_open(window):
            return SendResult(False, "ChatGPT's upgrade modal is open", rate_limited=True)
        return SendResult(False, "Could not find ChatGPT's message composer")

    message = (message or "").strip() or "continue"
    draft = _composer_text(composer)
    baseline = _count_sent_text(window, message)
    saved_clipboard = get_clipboard_text() if attachments else None

    try:
        if not _set_composer_text(composer, ""):
            return SendResult(False, "Could not clear ChatGPT's message composer")

        attached, failed = _paste_attachments(window, composer, attachments)
        composer = _find_composer(window) or composer

        if failed:
            return SendResult(False, f"Could not attach {failed!r} in ChatGPT")
        if attached:
            log.info("Attached %d image(s) in ChatGPT", attached)

        if not _set_composer_text(composer, message):
            try:
                composer.SetFocus()
                time.sleep(0.2)
            except Exception:
                pass
            composer = _find_composer(window) or composer
            if not _set_composer_text(composer, message):
                return SendResult(False, "Could not write the message into ChatGPT's composer")

        button = _find_send_button(window, composer)
        if button is None:
            return _blocked_result(window, "Could not find ChatGPT's Send button")
        if not _wait_for_enabled(button, 2.0):
            return _blocked_result(
                window,
                "ChatGPT's Send button is disabled; the rate limit is still active",
            )
        if not _invoke_or_click(button):
            return SendResult(False, "Could not click ChatGPT's Send button")

        result = _verify_sent(window, composer, message, baseline, 12.0)
        if result is None:
            # Exactly one click.  A second click would hit Stop and interrupt
            # the response that is already streaming.
            result = SendResult(
                True,
                "Message submitted (confirmation not readable yet)",
                submitted=True,
            )

        _restore_draft(window, draft, preserve_draft and not result.ok)
        return result
    finally:
        if saved_clipboard:
            set_clipboard_text(saved_clipboard)


def _blocked_result(window: auto.Control, reason: str) -> SendResult:
    snapshot = scan_window(window)
    if snapshot.limit is not None and limit_is_active(snapshot.limit):
        return SendResult(False, f"{reason}; the rate limit is still active", rate_limited=True)
    if is_modal_open(window):
        dismiss_modal(window, timeout=1.0)
        return SendResult(
            False,
            f"{reason}; the upgrade modal is open",
            rate_limited=True,
        )
    return SendResult(False, reason)


def _restore_draft(
    window: auto.Control,
    draft: str,
    preserve_draft: bool,
) -> None:
    if not preserve_draft or not draft:
        return
    try:
        composer = _find_composer(window)
        if composer is not None:
            _set_composer_text(composer, draft)
    except Exception:
        log.debug("Could not restore the ChatGPT draft", exc_info=True)


def _find_send_button(
    window: auto.Control,
    composer: auto.Control | None = None,
) -> auto.Control | None:
    candidates: list[auto.Control] = []
    for name in SEND_BUTTON_NAMES:
        try:
            direct = window.ButtonControl(Name=name, searchDepth=40)
            if direct.Exists(0.1):
                candidates.append(direct)
        except Exception:
            pass
    if not candidates:
        for control in _walk(window, max_depth=50, max_nodes=7000):
            try:
                if (
                    control.ControlType == auto.ControlType.ButtonControl
                    and (control.Name or "").strip() in SEND_BUTTON_NAMES
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


def _verify_sent(
    window: auto.Control,
    composer: auto.Control,
    message: str,
    baseline: int,
    timeout: float,
) -> SendResult | None:
    """Return a result when the send is confirmed or blocked, else None.

    ChatGPT renders the sent bubble as a text node with the message itself.
    Counting that exact text is much more reliable (and cheaper) than counting
    every "You said" label, which this build does not emit for new messages.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_modal_open(window):
            dismiss_modal(window, timeout=1.0)
            return SendResult(
                False,
                "ChatGPT showed the rate-limit/upgrade modal",
                rate_limited=True,
            )
        if _count_sent_text(window, message) > baseline:
            return _settle_after_send(window, "the message appeared in the chat")
        if _processing(window):
            return _settle_after_send(window, "ChatGPT started responding")

        current = _find_composer(window)
        text = _composer_text(current) if current is not None else ""
        if text:
            time.sleep(0.3)
            continue

        # The composer cleared.  Give the message a moment to render, then
        # confirm either the bubble or an empty composer with a disabled Send.
        time.sleep(0.8)
        if _count_sent_text(window, message) > baseline:
            return _settle_after_send(window, "the message appeared in the chat")
        if _processing(window):
            return _settle_after_send(window, "ChatGPT started responding")
        snapshot = scan_window(window)
        if snapshot.limit is not None and limit_is_active(snapshot.limit):
            return SendResult(
                False,
                "ChatGPT is still showing its rate limit",
                rate_limited=True,
            )
        if is_modal_open(window):
            dismiss_modal(window, timeout=1.0)
            return SendResult(
                False,
                "ChatGPT is showing a blocking dialog",
                rate_limited=True,
            )
        send_button = _find_send_button(window, current)
        if send_button is not None and not _is_enabled(send_button):
            return _settle_after_send(window, "the composer cleared")
        # A vanished composer or a still-enabled Send is not proof of a send.
        time.sleep(0.4)
    return None


def _settle_after_send(window: auto.Control, reason: str) -> SendResult:
    """Wait briefly for a blocking modal before declaring success.

    A limit banner that appears here belongs to the *next* episode: the send
    was already confirmed, so it must not be reported as a failed send.
    """

    time.sleep(0.7)
    for _ in range(2):
        if is_modal_open(window):
            dismiss_modal(window, timeout=1.0)
            return SendResult(
                False,
                "ChatGPT showed the rate-limit/upgrade modal",
                rate_limited=True,
            )
        time.sleep(0.3)
    return SendResult(True, f"Message sent ({reason})")


def _count_sent_text(window: auto.Control, message: str) -> int:
    """Count rendered copies of *message* in the conversation area.

    ChatGPT's sidebar is excluded by x-position, and the composer/footer by
    bottom position, so a chat literally named "continue" cannot inflate the
    count.
    """

    wanted = re.sub(r"\s+", " ", message or "").strip().lower()
    if not wanted:
        return 0
    first_line = re.sub(r"\s+", " ", (message or "").splitlines()[0]).strip().lower()
    prefix = first_line[:80]
    rect = get_window_rect(window)
    left_limit = rect[0] + 380
    bottom_limit = rect[3] - 96
    count = 0
    for control in _walk(window, max_depth=60, max_nodes=8000):
        try:
            if control.ControlType != auto.ControlType.TextControl:
                continue
            name = re.sub(r"\s+", " ", control.Name or "").strip().lower()
            if not name:
                continue
            if name == wanted:
                pass
            elif name == f"you said: {wanted}":
                pass
            elif name.startswith("you said:") and (
                wanted in name or (prefix and prefix in name)
            ):
                pass
            else:
                continue
            r = _control_rect(control)
            if r[2] <= r[0] or r[3] <= r[1]:
                continue
            if r[0] < left_limit or r[3] > bottom_limit:
                continue
            count += 1
        except Exception:
            continue
    return count


def _is_enabled(control: auto.Control) -> bool:
    try:
        return bool(control.IsEnabled)
    except Exception:
        return False


def _count_user_messages(window: auto.Control) -> int:
    count = 0
    for control in _walk(window, max_depth=60, max_nodes=8000):
        try:
            if control.ControlType != auto.ControlType.TextControl:
                continue
            name = re.sub(r"\s+", " ", (control.Name or "")).strip().lower()
            if name.startswith("you said") or " you said:" in name:
                count += 1
        except Exception:
            continue
    return count


def _paste_attachments(
    window: auto.Control,
    composer: auto.Control,
    attachments: tuple[str, ...],
) -> tuple[int, str | None]:
    """Paste images into ChatGPT's composer.  Returns (count, failed_path)."""

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


def _count_remove_buttons(window: auto.Control) -> int:
    count = 0
    for control in _walk(window, max_depth=55, max_nodes=8000):
        try:
            if control.ControlType != auto.ControlType.ButtonControl:
                continue
            name = (control.Name or "").strip().lower()
            if name.startswith("remove image") or name.startswith("remove file"):
                count += 1
        except Exception:
            continue
    return count
