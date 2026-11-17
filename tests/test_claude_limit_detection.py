from __future__ import annotations

import unittest
from datetime import datetime

from autoclaude_continue.claude_uia import TextNode, _choose_limit_text
from autoclaude_continue.rate_limit import limit_info


WINDOW = (0, 0, 1280, 900)


class SidePopupDetectionTests(unittest.TestCase):
    def test_split_side_pop_is_joined_and_keeps_the_reset_time(self):
        nodes = [
            TextNode("You've hit your limit for", 520, 700, 720, 720),
            TextNode("Claude messages.", 725, 700, 860, 720),
            TextNode("Limits will reset at 9:30 AM.", 520, 726, 750, 746),
        ]

        text = _choose_limit_text(nodes, False, WINDOW, {})

        self.assertIsNotNone(text)
        assert text is not None
        self.assertIn("Claude messages", text)
        self.assertIn("9:30 AM", text)
        info = limit_info(text, datetime(2026, 11, 15, 8, 0))
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.reset_at, datetime(2026, 11, 15, 9, 30))
        self.assertFalse(info.estimated)

    def test_normal_chat_paragraph_is_not_treated_as_a_side_pop(self):
        nodes = [
            TextNode("The message limit is described", 300, 300, 600, 320),
            TextNode("in the documentation.", 605, 300, 760, 320),
        ]

        self.assertIsNone(_choose_limit_text(nodes, False, WINDOW, {}))

    def test_toast_container_text_is_trusted(self):
        nodes = [
            TextNode(
                "Message limit reached.",
                980,
                180,
                1260,
                220,
                in_overlay=True,
            )
        ]

        text = _choose_limit_text(nodes, False, WINDOW, {})

        self.assertEqual(text, "Message limit reached.")


if __name__ == "__main__":
    unittest.main()
