from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

from autoclaude_continue import chatgpt_uia, claude_uia


class _Rect(SimpleNamespace):
    pass


class FakeWindow:
    def __init__(self, name: str, hwnd: int, width: int = 1000) -> None:
        self.Name = name
        self.ClassName = "Chrome_WidgetWin_1"
        self.NativeWindowHandle = hwnd
        self.IsOffscreen = False
        self.BoundingRectangle = _Rect(left=0, top=0, right=width, bottom=800)


def _discover(module, windows, allowed_hwnd):
    root = mock.Mock()
    root.GetChildren.return_value = windows
    patches = [
        mock.patch.object(module.auto, "GetRootControl", return_value=root),
        mock.patch.object(
            module,
            "window_belongs_to",
            side_effect=lambda hwnd, exe: hwnd == allowed_hwnd,
        ),
    ]
    if hasattr(module, "_user32"):
        patches.append(mock.patch.object(module, "_user32"))
    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(patch) for patch in patches]
        user32 = mocks[-1] if len(mocks) == 3 else None
        if user32 is not None:
            user32.IsWindow.return_value = True
        return module.find_window()


class ClaudeWindowDiscoveryTests(unittest.TestCase):
    def test_browser_tab_titled_claude_is_not_the_app(self):
        opera = FakeWindow(
            "Claude Just Got a HUGE Upgrade! - YouTube - Opera", 222, width=1400
        )
        claude = FakeWindow("Claude", 111)
        found = _discover(claude_uia, [opera, claude], allowed_hwnd=111)
        self.assertIs(found, claude)

    def test_no_real_app_returns_none(self):
        opera = FakeWindow(
            "Claude Just Got a HUGE Upgrade! - YouTube - Opera", 222, width=1400
        )
        self.assertIsNone(_discover(claude_uia, [opera], allowed_hwnd=111))


class ChatGPTWindowDiscoveryTests(unittest.TestCase):
    def test_browser_tab_titled_chatgpt_is_not_the_app(self):
        opera = FakeWindow("ChatGPT tips - YouTube - Opera", 222, width=1400)
        app = FakeWindow("ChatGPT", 111)
        found = _discover(chatgpt_uia, [opera, app], allowed_hwnd=111)
        self.assertIs(found, app)

    def test_no_real_app_returns_none(self):
        opera = FakeWindow("ChatGPT tips - YouTube - Opera", 222, width=1400)
        self.assertIsNone(_discover(chatgpt_uia, [opera], allowed_hwnd=111))


class ProcessIdentityTests(unittest.TestCase):
    def test_unknown_process_is_allowed(self):
        from autoclaude_continue import process_info

        with mock.patch.object(process_info, "window_process_name", return_value=""):
            self.assertTrue(process_info.window_belongs_to(123, "claude.exe"))

    def test_other_process_is_rejected(self):
        from autoclaude_continue import process_info

        with mock.patch.object(
            process_info, "window_process_name", return_value="opera.exe"
        ):
            self.assertFalse(process_info.window_belongs_to(123, "claude.exe"))


if __name__ == "__main__":
    unittest.main()
