from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

import uiautomation as auto

from autoclaude_continue import chatgpt_uia, claude_uia
from autoclaude_continue.claude_uia import UiSnapshot
from autoclaude_continue.rate_limit import LimitInfo


def _snapshot(limit: LimitInfo | None) -> UiSnapshot:
    return UiSnapshot(
        tree_ready=True,
        limit=limit,
        limit_text=limit.raw if limit else "",
        modal_open=False,
        composer_present=True,
        chat_title="Chat",
        window_rect=(0, 0, 100, 100),
    )


def _past_limit() -> LimitInfo:
    return LimitInfo(
        "out of messages until 9:00 AM",
        datetime.now() - timedelta(minutes=5),
    )


def _future_limit() -> LimitInfo:
    return LimitInfo(
        "out of messages until 11:00 AM",
        datetime.now() + timedelta(hours=1),
    )


class SettleAfterSendTests(unittest.TestCase):
    def _settle(self, module, limit: LimitInfo):
        with (
            mock.patch.object(module, "scan_window", return_value=_snapshot(limit)),
            mock.patch.object(module, "is_modal_open", return_value=False),
            mock.patch.object(module.time, "sleep"),
        ):
            return module._settle_after_send(object(), "the composer cleared")

    def test_stale_banner_is_not_reported_as_rate_limited(self):
        for module in (claude_uia, chatgpt_uia):
            with self.subTest(module=module.__name__):
                result = self._settle(module, _past_limit())
                self.assertTrue(result.ok)
                self.assertFalse(result.rate_limited)

    def test_active_banner_does_not_undo_a_confirmed_send(self):
        for module in (claude_uia, chatgpt_uia):
            with self.subTest(module=module.__name__):
                # This banner belongs to the next episode; the send was already
                # confirmed by the message/Stop button/composer state.
                result = self._settle(module, _future_limit())
                self.assertTrue(result.ok)
                self.assertFalse(result.rate_limited)


class PostSendFailureTests(unittest.TestCase):
    def _settle(self, limit: LimitInfo | None):
        with (
            mock.patch.object(
                claude_uia,
                "_count_failed_sends",
                return_value=1,
            ),
            mock.patch.object(
                claude_uia,
                "scan_window",
                return_value=_snapshot(limit),
            ),
            mock.patch.object(claude_uia, "is_modal_open", return_value=False),
            mock.patch.object(claude_uia.time, "sleep"),
        ):
            return claude_uia._settle_after_send(
                object(),
                "the message appeared in the chat",
                baseline_failures=0,
            )

    def test_failed_send_with_side_pop_is_rate_limited(self):
        result = self._settle(_future_limit())
        self.assertFalse(result.ok)
        self.assertTrue(result.rate_limited)
        self.assertTrue(result.keep_composer)

    def test_failed_send_without_limit_keeps_the_restored_draft(self):
        result = self._settle(None)
        self.assertFalse(result.ok)
        self.assertFalse(result.rate_limited)
        self.assertTrue(result.keep_composer)

    def test_failed_send_marker_is_counted(self):
        class Node:
            Name = "Failed to send"
            ControlType = auto.ControlType.TextControl

        with mock.patch.object(claude_uia, "_walk", return_value=[Node()]):
            self.assertEqual(claude_uia._count_failed_sends(object()), 1)


class VerifyRestoredDraftTests(unittest.TestCase):
    def test_restored_composer_with_side_pop_is_rate_limited(self):
        clock = {"now": 0.0}

        def sleep(seconds: float) -> None:
            clock["now"] += seconds

        def monotonic() -> float:
            return clock["now"]

        with (
            mock.patch.object(claude_uia, "is_modal_open", return_value=False),
            mock.patch.object(claude_uia, "_count_send_markers", return_value=(0, 0)),
            mock.patch.object(claude_uia, "_find_composer", return_value=object()),
            mock.patch.object(
                claude_uia,
                "_composer_text",
                return_value="By looking at the reference image...",
            ),
            mock.patch.object(
                claude_uia,
                "scan_window",
                return_value=_snapshot(_future_limit()),
            ),
            mock.patch.object(claude_uia, "_processing", return_value=False),
            mock.patch.object(claude_uia.time, "sleep", side_effect=sleep),
            mock.patch.object(
                claude_uia.time,
                "monotonic",
                side_effect=monotonic,
            ),
        ):
            result = claude_uia._verify_sent(
                object(),
                object(),
                "By looking at the reference image...",
                0,
                1.0,
                baseline_failures=0,
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.ok)
        self.assertTrue(result.rate_limited)
        self.assertTrue(result.keep_composer)


class BlockedResultTests(unittest.TestCase):
    def _blocked(self, module, limit: LimitInfo):
        with (
            mock.patch.object(module, "scan_window", return_value=_snapshot(limit)),
            mock.patch.object(module, "is_modal_open", return_value=False),
        ):
            return module._blocked_result(object(), "Send button missing")

    def test_stale_banner_does_not_mark_the_send_as_rate_limited(self):
        for module in (claude_uia, chatgpt_uia):
            with self.subTest(module=module.__name__):
                result = self._blocked(module, _past_limit())
                self.assertFalse(result.ok)
                self.assertFalse(result.rate_limited)

    def test_active_banner_still_marks_the_send_as_rate_limited(self):
        for module in (claude_uia, chatgpt_uia):
            with self.subTest(module=module.__name__):
                result = self._blocked(module, _future_limit())
                self.assertFalse(result.ok)
                self.assertTrue(result.rate_limited)


class VerifySentTests(unittest.TestCase):
    def _verify(self, module, limit: LimitInfo, count_name: str):
        clock = {"now": 0.0}

        def sleep(seconds: float) -> None:
            clock["now"] += seconds

        def monotonic() -> float:
            return clock["now"]

        with (
            mock.patch.object(module, "is_modal_open", return_value=False),
            mock.patch.object(module, count_name, return_value=0),
            mock.patch.object(module, "_find_composer", return_value=object()),
            mock.patch.object(module, "_composer_text", return_value=""),
            mock.patch.object(module, "scan_window", return_value=_snapshot(limit)),
            mock.patch.object(module, "_find_send_button", return_value=None),
            mock.patch.object(module, "_processing", return_value=False),
            mock.patch.object(module.time, "sleep", side_effect=sleep),
            mock.patch.object(module.time, "monotonic", side_effect=monotonic),
        ):
            return module._verify_sent(object(), object(), "continue", 0, 1.0)

    def test_claude_stale_banner_does_not_stop_verification(self):
        result = self._verify(claude_uia, _past_limit(), "_count_sent_messages")
        self.assertIsNone(result)

    def test_claude_active_banner_stops_verification(self):
        result = self._verify(claude_uia, _future_limit(), "_count_sent_messages")
        self.assertIsNotNone(result)
        self.assertTrue(result.rate_limited)

    def test_chatgpt_stale_banner_does_not_stop_verification(self):
        result = self._verify(chatgpt_uia, _past_limit(), "_count_sent_text")
        self.assertIsNone(result)

    def test_chatgpt_active_banner_stops_verification(self):
        result = self._verify(chatgpt_uia, _future_limit(), "_count_sent_text")
        self.assertIsNotNone(result)
        self.assertTrue(result.rate_limited)


class ProcessingTests(unittest.TestCase):
    class Rect:
        left = 900
        top = 700
        right = 980
        bottom = 760

    def _button(self, name: str, offscreen: bool = False):
        class Button:
            ControlType = auto.ControlType.ButtonControl
            IsOffscreen = offscreen
            BoundingRectangle = self.Rect()

        Button.Name = name
        return Button()

    def _processing(self, control) -> bool:
        with (
            mock.patch.object(claude_uia, "_walk", return_value=[control]),
            mock.patch.object(
                claude_uia, "get_window_rect", return_value=(0, 0, 1000, 1000)
            ),
        ):
            return claude_uia._processing(object())

    def test_stop_button_names(self):
        for name in ("Stop", "Stop response", "Stop generating"):
            with self.subTest(name=name):
                self.assertTrue(claude_uia.is_stop_button(name))
        self.assertFalse(claude_uia.is_stop_button("Send message"))

    def test_visible_stop_button_means_processing(self):
        self.assertTrue(self._processing(self._button("Stop response")))

    def test_hidden_stop_button_is_ignored(self):
        self.assertFalse(
            self._processing(self._button("Stop response", offscreen=True))
        )

    def test_sidebar_chat_named_stop_is_ignored(self):
        class SidebarButton:
            Name = "Stop"
            ControlType = auto.ControlType.ButtonControl
            IsOffscreen = False

            class BoundingRectangle:
                left = 20
                top = 300
                right = 180
                bottom = 330

        self.assertFalse(self._processing(SidebarButton()))


class TextMatchTests(unittest.TestCase):
    def test_multiline_message_matches_the_truncated_first_line(self):
        class Node:
            Name = "You said: continue but heres some issues I saw:"
            ControlType = auto.ControlType.TextControl

        message = (
            "continue but heres some issues I saw:\n"
            "The soft place system doesn't always work..."
        )
        with mock.patch.object(claude_uia, "_walk", return_value=[Node()]):
            count = claude_uia._count_sent_messages(object(), message)
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
