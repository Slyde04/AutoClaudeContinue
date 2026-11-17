from __future__ import annotations

import unittest
from unittest import mock

from autoclaude_continue import claude_uia


class LocateChatRetryTests(unittest.TestCase):
    """The sidebar re-renders after a chat switch; a scan may come up empty."""

    def test_missing_chat_is_rescanned_before_giving_up(self):
        control = object()
        with mock.patch.object(
            claude_uia,
            "_find_chat_button",
            side_effect=[None, None, control],
        ) as finder, mock.patch.object(claude_uia.time, "sleep") as sleep:
            found = claude_uia._locate_chat(
                object(), "Loop pad music webapp", attempts=3, delay=0.5
            )
        self.assertIs(found, control)
        self.assertEqual(finder.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_retries_stop_at_the_requested_attempts(self):
        with mock.patch.object(
            claude_uia, "_find_chat_button", return_value=None
        ) as finder, mock.patch.object(claude_uia.time, "sleep") as sleep:
            found = claude_uia._locate_chat(
                object(), "missing chat", attempts=2, delay=0.4
            )
        self.assertIsNone(found)
        self.assertEqual(finder.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_first_scan_hit_does_not_sleep(self):
        control = object()
        with mock.patch.object(
            claude_uia, "_find_chat_button", return_value=control
        ), mock.patch.object(claude_uia.time, "sleep") as sleep:
            found = claude_uia._locate_chat(object(), "chat")
        self.assertIs(found, control)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
