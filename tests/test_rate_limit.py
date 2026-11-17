from __future__ import annotations

import unittest
from datetime import datetime

from autoclaude_continue.rate_limit import (
    format_duration,
    format_reset_time,
    limit_info,
    looks_like_limit,
    parse_reset_time,
)


NOW = datetime(2026, 11, 9, 22, 41)  # Monday


class ResetParserTests(unittest.TestCase):
    def assertReset(self, text: str, expected: datetime, now: datetime = NOW) -> None:
        actual = parse_reset_time(text, now)
        self.assertEqual(
            actual,
            expected,
            f"{text!r} parsed as {actual!r}, expected {expected!r}",
        )

    def test_banner_tomorrow(self):
        self.assertReset(
            "You are out of free messages until tomorrow at 2:30 AM Upgrade",
            datetime(2026, 11, 10, 2, 30),
        )

    def test_modal_resets_at(self):
        self.assertReset(
            "You hit your 5-hour message limit. It resets at 2:30 AM, "
            "or you can upgrade for higher limits.",
            datetime(2026, 11, 10, 2, 30),
        )

    def test_free_messages_return(self):
        self.assertReset(
            "Your free messages return at 2:30 AM, or keep going now with Pro.",
            datetime(2026, 11, 10, 2, 30),
        )

    def test_past_reset_is_not_moved_to_tomorrow(self):
        # The wake timer fires a few seconds after the reset; the banner may
        # still be visible.  It must be treated as already past, not as the
        # same clock time tomorrow.
        self.assertReset(
            "You are out of free messages until 1:10 AM Upgrade",
            datetime(2026, 11, 11, 1, 10),
            now=datetime(2026, 11, 11, 1, 10, 11),
        )

    def test_recent_past_reset_survives_a_late_check(self):
        self.assertReset(
            "Your free messages return at 12:20 PM.",
            datetime(2026, 11, 11, 12, 20),
            now=datetime(2026, 11, 11, 13, 5),
        )

    def test_far_past_clock_time_rolls_to_tomorrow(self):
        self.assertReset(
            "You are out of free messages until 1:10 AM Upgrade",
            datetime(2026, 11, 12, 1, 10),
            now=datetime(2026, 11, 11, 12, 0),
        )

    def test_tomorrow_after_time(self):
        self.assertReset(
            "Resets at 2:30 AM tomorrow.",
            datetime(2026, 11, 10, 2, 30),
        )

    def test_tonight(self):
        self.assertReset(
            "Resets at 11:00 PM today.",
            datetime(2026, 11, 9, 23, 0),
        )

    def test_next_weekday(self):
        self.assertReset(
            "Available again Monday at 9:00 AM.",
            datetime(2026, 11, 16, 9, 0),
        )

    def test_tomorrow_weekday(self):
        self.assertReset(
            "Available again Tuesday at 9:00 AM.",
            datetime(2026, 11, 10, 9, 0),
        )

    def test_next_weekday_prefix(self):
        self.assertReset(
            "Resets next Monday at 9:00 AM.",
            datetime(2026, 11, 16, 9, 0),
        )

    def test_month_day(self):
        self.assertReset(
            "Resets on Nov 10 at 2:30 AM.",
            datetime(2026, 11, 10, 2, 30),
        )

    def test_month_day_24h(self):
        self.assertReset(
            "Resets on November 10 at 14:30.",
            datetime(2026, 11, 10, 14, 30),
        )

    def test_month_day_with_year(self):
        self.assertReset(
            "Resets on Dec 1, 2026 at 6:00 PM.",
            datetime(2026, 12, 1, 18, 0),
        )

    def test_relative_minutes(self):
        self.assertReset(
            "Try again in 30 minutes.",
            datetime(2026, 11, 9, 23, 11),
        )

    def test_relative_hours(self):
        self.assertReset(
            "It resets in 2 hours.",
            datetime(2026, 11, 10, 0, 41),
        )

    def test_relative_compound(self):
        self.assertReset(
            "Resets in 1 hour 30 minutes.",
            datetime(2026, 11, 10, 0, 11),
        )

    def test_relative_short_units(self):
        self.assertReset(
            "Resets in 2h.",
            datetime(2026, 11, 10, 0, 41),
        )
        self.assertReset(
            "Resets in 90m.",
            datetime(2026, 11, 10, 0, 11),
        )

    def test_relative_half_hour(self):
        self.assertReset(
            "Try again in half an hour.",
            datetime(2026, 11, 9, 23, 11),
        )

    def test_relative_an_hour(self):
        self.assertReset(
            "Try again in an hour.",
            datetime(2026, 11, 9, 23, 41),
        )

    def test_relative_after(self):
        self.assertReset(
            "Available after 2 hours.",
            datetime(2026, 11, 10, 0, 41),
        )

    def test_24h_time(self):
        self.assertReset(
            "Resets at 14:30.",
            datetime(2026, 11, 10, 14, 30),
        )

    def test_24h_time_early_morning(self):
        self.assertReset(
            "Resets at 02:30.",
            datetime(2026, 11, 10, 2, 30),
        )

    def test_ambiguous_time_nearest(self):
        self.assertReset(
            "Resets at 12:30.",
            datetime(2026, 11, 10, 0, 30),
        )

    def test_ampm_dots(self):
        self.assertReset(
            "Resets at 2:30 a.m.",
            datetime(2026, 11, 10, 2, 30),
        )

    def test_pm(self):
        self.assertReset(
            "Resets at 2:30 PM.",
            datetime(2026, 11, 10, 14, 30),
        )

    def test_noon(self):
        self.assertReset(
            "Resets at 12:30 PM.",
            datetime(2026, 11, 10, 12, 30),
        )

    def test_midnight(self):
        self.assertReset(
            "Resets at 12:30 AM.",
            datetime(2026, 11, 10, 0, 30),
        )

    def test_five_hour_phrase_is_not_relative(self):
        self.assertReset(
            "You hit your 5-hour message limit. It resets at 2:30 AM.",
            datetime(2026, 11, 10, 2, 30),
        )

    def test_no_time(self):
        self.assertIsNone(parse_reset_time("You are out of free messages.", NOW))
        self.assertIsNone(parse_reset_time("Something else entirely.", NOW))

    def test_developer_side_pop_reset(self):
        self.assertReset(
            "You've hit your limit for Claude messages. "
            "Limits will reset at 9:30 AM.",
            datetime(2026, 11, 9, 9, 30),
            now=datetime(2026, 11, 9, 8, 0),
        )

    def test_available_now(self):
        self.assertReset("Messages available now.", NOW)


class LimitDetectionTests(unittest.TestCase):
    def test_banner(self):
        self.assertTrue(
            looks_like_limit(
                "You are out of free messages until tomorrow at 2:30 AM Upgrade"
            )
        )

    def test_modal(self):
        self.assertTrue(
            looks_like_limit(
                "You hit your 5-hour message limit. It resets at 2:30 AM."
            )
        )

    def test_return(self):
        self.assertTrue(
            looks_like_limit("Your free messages return at 2:30 AM.")
        )

    def test_normal_chat_text(self):
        self.assertFalse(looks_like_limit("The rate limit resets at 2:30 AM."))
        self.assertFalse(looks_like_limit("Let's continue this conversation."))
        self.assertFalse(looks_like_limit("Can you explain how the API rate limit works?"))
        self.assertFalse(looks_like_limit("I need to limit my response length."))

    def test_developer_side_pop(self):
        self.assertTrue(
            looks_like_limit(
                "You've hit your limit for Claude messages. "
                "Limits will reset at 9:30 AM."
            )
        )
        self.assertTrue(looks_like_limit("Please wait before trying again."))

    def test_chatgpt_banners(self):
        self.assertTrue(
            looks_like_limit(
                "You've reached the free plan limit. Your limit will reset at 3:12 PM."
            )
        )
        self.assertTrue(
            looks_like_limit("You've hit the plus plan limit. Limit resets in 2 hours.")
        )
        self.assertTrue(
            looks_like_limit("You can send messages again tomorrow at 9:00 AM.")
        )
        self.assertTrue(
            looks_like_limit("You've reached your usage cap for GPT-5.")
        )

    def test_chatgpt_normal_text(self):
        self.assertFalse(
            looks_like_limit(
                "The documentation says the API rate limit resets at 2:30 AM."
            )
        )
        self.assertFalse(
            looks_like_limit("The free plan is generous with messages.")
        )

    def test_limit_info(self):
        info = limit_info(
            "You are out of free messages until tomorrow at 2:30 AM Upgrade",
            NOW,
        )
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.reset_at, datetime(2026, 11, 10, 2, 30))
        self.assertFalse(info.estimated)

    def test_limit_info_without_time(self):
        info = limit_info("You are out of free messages.", NOW)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertIsNone(info.reset_at)
        self.assertTrue(info.estimated)


class FormattingTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(59), "59s")
        self.assertEqual(format_duration(60), "1m 0s")
        self.assertEqual(format_duration(3661), "1h 1m 1s")
        self.assertEqual(format_duration(90000), "1d 1h 0m")

    def test_reset_time(self):
        self.assertEqual(
            format_reset_time(datetime(2026, 11, 10, 2, 30), NOW),
            "tomorrow at 2:30 AM",
        )
        self.assertEqual(
            format_reset_time(datetime(2026, 11, 9, 23, 0), NOW),
            "today at 11:00 PM",
        )


if __name__ == "__main__":
    unittest.main()
