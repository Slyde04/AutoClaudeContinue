from __future__ import annotations

import queue
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta
from unittest import mock

from autoclaude_continue import automator
from autoclaude_continue.automator import Automator, _ScanResult, _limit_is_active
from autoclaude_continue.claude_uia import SendResult, UiSnapshot
from autoclaude_continue.config import Config
from autoclaude_continue.rate_limit import LimitInfo


NOW = datetime(2026, 11, 13, 10, 0)


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


class FakeModule:
    def focus_window(self, window):
        return None

    def restore_focus(self, state):
        pass


class FakeAdapter:
    key = "fake"
    display_name = "Fake"
    module = FakeModule()

    def __init__(
        self,
        limit: LimitInfo | None,
        result: SendResult | None = None,
        processing: bool = False,
    ) -> None:
        self.limit = limit
        self.result = result
        self.processing = processing
        self.post_send_limit: LimitInfo | None = None
        self.sent = 0
        self.selections: list[str] = []

    def find_window(self):
        return None

    def dismiss_modal(self, window, timeout=1.5) -> bool:
        return True

    def scan_window(self, window) -> UiSnapshot:
        limit = self.post_send_limit if self.sent and self.post_send_limit is not None else self.limit
        snapshot = _snapshot(limit)
        snapshot.processing = self.processing
        return snapshot

    def select_chat(self, window, name):
        self.selections.append(name)
        return True, f"Selected chat {name!r}"

    def send_message(self, window, message, attachments=(), preserve_draft=True):
        self.sent += 1
        return self.result or SendResult(True, "sent")


class Harness(Automator):
    """Minimal Automator with the UI-automation side effects stubbed out."""

    def __init__(self, limit: LimitInfo) -> None:
        self._pending_limit = limit
        self._pending_adapter = None
        self._last_sent = {}
        self._answered = {}
        self._reset_retries = {}
        self._next_timeout = 0
        self._manual_send = False
        self._send_retry_at = None
        self._send_failures = 0
        self._scheduled_for = None
        self._settle_until = 0.0
        self._probe_pending = False
        self._claude_open = False
        self.events = queue.Queue()
        self.commands = queue.Queue()
        self.store = FakeStore(Config())
        self.failed = None
        self.handled = None

    def _reset_pending(self) -> None:
        pass

    def _cancel_schedule(self) -> None:
        pass

    def _send_failed(self, adapter, message, config) -> None:
        self.failed = message

    def _handle_limit(self, adapter, info, config) -> None:
        self.handled = info


class FakeStore:
    def __init__(self, config: Config) -> None:
        self.config = config

    def get(self) -> Config:
        return self.config

    def update(self, **changes) -> Config:
        current = asdict(self.config)
        current.update({key: value for key, value in changes.items() if key in current})
        self.config = Config(**current).validated()
        return self.config


class SendGateTests(unittest.TestCase):
    def test_past_limit_does_not_block_the_send(self):
        limit = LimitInfo(
            "out of messages until 9:00 AM",
            datetime.now() - timedelta(minutes=5),
        )
        harness = Harness(limit)
        adapter = FakeAdapter(limit)
        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._send_continue(adapter, object(), Config())
        self.assertEqual(adapter.sent, 1)
        self.assertIsNone(harness.handled)
        self.assertIsNone(harness.failed)

    def test_future_limit_still_blocks_the_send(self):
        limit = LimitInfo(
            "out of messages until 11:00 AM",
            datetime.now() + timedelta(hours=1),
        )
        harness = Harness(limit)
        adapter = FakeAdapter(limit)
        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._send_continue(adapter, object(), Config())
        self.assertEqual(adapter.sent, 0)
        self.assertIs(harness.handled, limit)

    def test_past_limit_still_selects_the_requested_chat(self):
        limit = LimitInfo(
            "out of messages until 9:00 AM",
            datetime.now() - timedelta(minutes=5),
        )
        harness = Harness(limit)
        adapter = FakeAdapter(limit)
        config = Config(chat_name="Loop pad music webapp")
        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._send_continue(adapter, object(), config)
        self.assertEqual(adapter.selections, ["Loop pad music webapp"])
        self.assertEqual(adapter.sent, 1)


class ManualSendTests(unittest.TestCase):
    def test_manual_send_bypasses_an_active_limit(self):
        limit = LimitInfo(
            "out of messages until 11:00 AM",
            datetime.now() + timedelta(hours=1),
        )
        harness = Harness(limit)
        adapter = FakeAdapter(limit)
        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._send_continue(adapter, object(), Config(), manual=True)
        self.assertEqual(adapter.sent, 1)
        self.assertIsNone(harness.handled)

    def test_manual_send_without_a_window_reports_not_found(self):
        limit = LimitInfo(
            "out of messages until 11:00 AM",
            datetime.now() + timedelta(hours=1),
        )
        harness = Harness(limit)
        adapter = FakeAdapter(limit)
        harness._pending_adapter = adapter
        harness._window_for_send = lambda _adapter, _config: None

        harness._send_now(Config(app_target="auto"))

        self.assertEqual(adapter.sent, 0)
        states = []
        while not harness.events.empty():
            event = harness.events.get_nowait()
            if event.get("type") == "state":
                states.append(event["state"])
        self.assertIn("not_found", states)

    def test_check_now_marks_the_next_tick_as_a_manual_send(self):
        limit = LimitInfo("limit", datetime.now() + timedelta(hours=1))
        harness = Harness(limit)
        harness.commands.put(("check_now", {}))

        harness._process_commands()

        self.assertTrue(harness._manual_send)

    def test_manual_send_runs_even_when_detection_is_paused(self):
        limit = LimitInfo("limit", datetime.now() + timedelta(hours=1))
        harness = Harness(limit)
        adapter = FakeAdapter(limit)
        harness._pending_adapter = adapter
        harness.store = FakeStore(Config(detection_enabled=False, app_target="auto"))
        harness._window_for_send = lambda _adapter, _config: object()
        harness._manual_send = True

        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._tick()

        self.assertEqual(adapter.sent, 1)


class PendingSendTests(unittest.TestCase):
    def test_past_reset_sends_directly_without_scanning(self):
        limit = LimitInfo(
            "out of messages until 9:00 AM",
            datetime.now() - timedelta(minutes=1),
        )
        harness = Harness(limit)
        adapter = FakeAdapter(limit)
        harness._pending_adapter = adapter
        harness.store = FakeStore(Config(app_target="auto"))
        harness._window_for_send = lambda _adapter, _config: object()
        harness._scan_one = mock.Mock(
            side_effect=AssertionError("a past-due reset must send before scanning")
        )

        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._tick()

        self.assertEqual(adapter.sent, 1)
        self.assertIsNone(harness.handled)
        harness._scan_one.assert_not_called()


class DuplicateSendTests(unittest.TestCase):
    def test_submitted_but_unconfirmed_send_is_not_retried(self):
        limit = LimitInfo(
            "out of messages until 9:00 AM",
            datetime.now() - timedelta(minutes=1),
        )
        harness = Harness(limit)
        adapter = FakeAdapter(
            limit,
            result=SendResult(
                True,
                "Message submitted (confirmation not readable yet)",
                submitted=True,
            ),
        )
        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._send_continue(adapter, object(), Config())

        self.assertEqual(adapter.sent, 1)
        self.assertIsNone(harness.failed)
        self.assertEqual(
            harness._answered[adapter.key],
            automator._episode_key(limit),
        )

    def test_processing_blocks_the_send(self):
        limit = LimitInfo(
            "out of messages until 9:00 AM",
            datetime.now() - timedelta(minutes=1),
        )
        harness = Harness(limit)
        adapter = FakeAdapter(limit, processing=True)
        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._send_continue(adapter, object(), Config())

        self.assertEqual(adapter.sent, 0)
        self.assertIsNone(harness.failed)
        states = []
        while not harness.events.empty():
            event = harness.events.get_nowait()
            if event.get("type") == "state":
                states.append(event["state"])
        self.assertIn("waiting", states)

    def test_new_future_banner_after_send_stays_unanswered(self):
        past = LimitInfo(
            "out of messages until 9:00 AM",
            datetime.now() - timedelta(minutes=1),
        )
        future = LimitInfo(
            "out of messages until 11:00 PM",
            datetime.now() + timedelta(hours=2),
        )
        harness = Harness(past)
        adapter = FakeAdapter(past, result=SendResult(True, "sent"))
        adapter.post_send_limit = future
        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._send_continue(adapter, object(), Config())

        # The 9:00 AM episode was answered; the 11:00 PM banner is a new one
        # and must still trigger a send later.
        self.assertTrue(harness._recently_sent(adapter.key, past))
        self.assertFalse(harness._recently_sent(adapter.key, future))

    def test_answered_episode_survives_until_the_banner_clears(self):
        limit = LimitInfo(
            "out of messages until 9:00 AM",
            datetime.now() - timedelta(minutes=1),
        )
        harness = Harness(limit)
        adapter = FakeAdapter(limit)
        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._send_continue(adapter, object(), Config())

        # The hourly poll still sees the stale banner hours later; the guard
        # must not be fooled by the passage of time.
        self.assertTrue(harness._recently_sent(adapter.key, limit))
        harness._clear_answered(
            [_ScanResult(adapter, object(), _snapshot(None))]
        )
        self.assertFalse(harness._recently_sent(adapter.key, limit))


class DraftResetTests(unittest.TestCase):
    def _send(self, config: Config, result: SendResult | None = None):
        limit = LimitInfo(
            "out of messages until 9:00 AM",
            datetime.now() - timedelta(minutes=1),
        )
        harness = Harness(limit)
        harness.store = FakeStore(config)
        adapter = FakeAdapter(limit, result=result)
        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._send_continue(adapter, object(), config)
        return harness

    def test_custom_sent_message_becomes_a_preset_and_the_draft_resets(self):
        from pathlib import Path

        config = Config(
            message="please continue with the UI",
            saved_messages=("continue",),
            attachments=(str(Path("one.png")),),
        )
        harness = self._send(config)

        stored = harness.store.get()
        self.assertEqual(stored.message, "continue")
        self.assertEqual(stored.attachments, ())
        self.assertIn("please continue with the UI", stored.saved_messages)

        resets = []
        while not harness.events.empty():
            event = harness.events.get_nowait()
            if event.get("type") == "draft_reset":
                resets.append(event)
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0]["message"], "continue")
        self.assertEqual(resets[0]["attachments"], [])

    def test_plain_continue_send_leaves_the_draft_alone(self):
        config = Config(message="continue", saved_messages=("continue",))
        harness = self._send(config)

        stored = harness.store.get()
        self.assertEqual(stored.message, "continue")
        self.assertEqual(stored.saved_messages, ("continue",))

    def test_failed_send_does_not_save_a_preset(self):
        config = Config(
            message="this did not send",
            saved_messages=("continue",),
        )
        harness = self._send(
            config,
            result=SendResult(False, "Could not find Claude's Send button"),
        )

        stored = harness.store.get()
        self.assertEqual(stored.message, "this did not send")
        self.assertNotIn("this did not send", stored.saved_messages)

    def test_continue_with_images_clears_the_attachments(self):
        from pathlib import Path

        config = Config(
            message="continue",
            attachments=(str(Path("one.png")),),
        )
        harness = self._send(config)

        stored = harness.store.get()
        self.assertEqual(stored.message, "continue")
        self.assertEqual(stored.attachments, ())


class PostSendLimitTests(unittest.TestCase):
    def _send(self, result: SendResult, post_send_limit: LimitInfo | None):
        limit = LimitInfo(
            "out of messages until 9:00 AM",
            datetime.now() - timedelta(minutes=1),
        )
        harness = Harness(limit)
        adapter = FakeAdapter(limit, result=result)
        adapter.post_send_limit = post_send_limit
        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._send_continue(adapter, object(), Config())
        return harness, adapter

    def test_post_send_side_pop_is_handled_as_a_rate_limit(self):
        future = LimitInfo(
            "out of messages until 1:20 AM",
            datetime.now() + timedelta(hours=2),
        )
        harness, adapter = self._send(
            SendResult(
                False,
                "Claude reported that the message failed to send; "
                "Claude's rate limit appeared after sending",
                rate_limited=True,
                keep_composer=True,
            ),
            future,
        )

        self.assertEqual(adapter.sent, 1)
        self.assertIs(harness.handled, future)
        self.assertIsNone(harness.failed)

    def test_generic_failure_still_finds_the_post_send_side_pop(self):
        future = LimitInfo(
            "out of messages until 1:20 AM",
            datetime.now() + timedelta(hours=2),
        )
        harness, adapter = self._send(
            SendResult(
                False,
                "Claude reported that the message failed to send; "
                "Claude restored the message and images",
                keep_composer=True,
            ),
            future,
        )

        self.assertEqual(adapter.sent, 1)
        self.assertIs(harness.handled, future)
        self.assertIsNone(harness.failed)

    def test_stale_side_pop_after_a_generic_failure_allows_a_retry(self):
        past = LimitInfo(
            "out of messages until 8:00 AM",
            datetime.now() - timedelta(minutes=5),
        )
        harness, _adapter = self._send(
            SendResult(
                False,
                "Claude reported that the message failed to send",
                keep_composer=True,
            ),
            past,
        )

        self.assertIsNone(harness.handled)
        self.assertEqual(
            harness.failed,
            "Claude reported that the message failed to send",
        )

    def test_post_send_failure_preserves_the_configured_draft_and_images(self):
        from pathlib import Path

        config = Config(
            message="look at this reference image",
            attachments=(str(Path("one.png")),),
            saved_messages=("continue",),
        )
        limit = LimitInfo(
            "out of messages until 9:00 AM",
            datetime.now() - timedelta(minutes=1),
        )
        harness = Harness(limit)
        harness.store = FakeStore(config)
        adapter = FakeAdapter(
            limit,
            result=SendResult(
                False,
                "Claude reported that the message failed to send; "
                "Claude's rate limit appeared after sending",
                rate_limited=True,
                keep_composer=True,
            ),
        )
        adapter.post_send_limit = LimitInfo(
            "out of messages until 1:20 AM",
            datetime.now() + timedelta(hours=2),
        )

        with (
            mock.patch.object(automator, "focus_is_safe", return_value=(True, "")),
            mock.patch.object(harness, "_reset_draft_after_send") as reset,
        ):
            harness._send_continue(adapter, object(), config)

        reset.assert_not_called()
        stored = harness.store.get()
        self.assertEqual(stored.message, config.message)
        self.assertEqual(stored.attachments, config.attachments)
        self.assertNotIn(config.message, stored.saved_messages)

    def test_successful_send_rechecks_for_a_new_limit_after_30_seconds(self):
        past = LimitInfo(
            "out of messages until 9:00 AM",
            datetime.now() - timedelta(minutes=1),
        )
        harness = Harness(past)
        adapter = FakeAdapter(past, result=SendResult(True, "sent"))

        with mock.patch.object(automator, "focus_is_safe", return_value=(True, "")):
            harness._send_continue(adapter, object(), Config())

        self.assertEqual(harness._next_timeout, automator.POST_SEND_RECHECK_SECONDS)
        self.assertEqual(harness._next_timeout, 30.0)


class ProbeHarness(Harness):
    def __init__(self, config: Config) -> None:
        super().__init__(None)
        self.store = FakeStore(config)
        self._probe_pending = True
        self._claude_open = False
        self._dev_mode_seen = False
        self.scans: list[_ScanResult] = []
        self.probes: list[str] = []
        self.pending_handled = 0
        self.idle = 0

    def _scan_all(self, config):
        return self.scans

    def _emit_idle(self, scans, config) -> None:
        self.idle += 1

    def _send_continue(self, adapter, window, config) -> None:
        self.probes.append(adapter.key)

    def _handle_pending_send(self, config) -> None:
        self.pending_handled += 1


def _claude_scan() -> _ScanResult:
    adapter = FakeAdapter(None)
    adapter.key = "claude"
    return _ScanResult(adapter, object(), _snapshot(None))


class DeveloperModeProbeTests(unittest.TestCase):
    def config(self, **changes) -> Config:
        values = {
            "detection_enabled": True,
            "claude_dev_mode": True,
            "app_target": "claude",
            "poll_seconds": 900,
        }
        values.update(changes)
        return Config(**values)

    def test_startup_waits_without_stealing_focus_before_claude_opens(self):
        harness = ProbeHarness(self.config())
        harness._tick()
        self.assertTrue(harness._probe_pending)
        self.assertFalse(harness._claude_open)
        self.assertEqual(harness.probes, [])
        self.assertLessEqual(harness._next_timeout, 20.0)

    def test_startup_probe_sends_once_when_claude_is_ready(self):
        harness = ProbeHarness(self.config())
        harness.scans = [_claude_scan()]
        harness._tick()
        self.assertEqual(harness.probes, ["claude"])
        self.assertFalse(harness._probe_pending)
        self.assertTrue(harness._claude_open)

    def test_closing_and_reopening_claude_rearms_the_probe(self):
        harness = ProbeHarness(self.config())
        harness.scans = [_claude_scan()]
        harness._tick()
        harness.scans = []
        harness._tick()
        self.assertFalse(harness._probe_pending)
        self.assertFalse(harness._claude_open)
        harness.scans = [_claude_scan()]
        harness._tick()
        self.assertEqual(harness.probes, ["claude", "claude"])

    def test_disabled_developer_mode_never_uses_a_probe(self):
        harness = ProbeHarness(self.config(claude_dev_mode=False))
        harness.scans = [_claude_scan()]
        harness._tick()
        self.assertEqual(harness.probes, [])
        self.assertTrue(harness._probe_pending)

    def test_a_pending_continuation_send_wins_over_the_probe(self):
        harness = ProbeHarness(self.config())
        harness._pending_adapter = FakeAdapter(None)
        harness._send_retry_at = datetime.now() + timedelta(hours=1)
        harness.scans = [_claude_scan()]
        harness._tick()
        self.assertEqual(harness.probes, [])
        self.assertEqual(harness.pending_handled, 1)
        self.assertTrue(harness._probe_pending)

    def test_enabling_developer_mode_rearms_the_probe(self):
        harness = ProbeHarness(self.config(claude_dev_mode=False))
        harness._probe_pending = False
        harness._claude_open = True
        harness.scans = [_claude_scan()]
        harness.commands.put(("config_changed", {}))
        harness.store.update(claude_dev_mode=True)
        harness._process_commands()
        harness._tick()
        self.assertEqual(harness.probes, ["claude"])


class LimitActiveTests(unittest.TestCase):
    def test_future_reset_is_active(self):
        info = LimitInfo("limit", NOW + timedelta(minutes=30))
        self.assertTrue(_limit_is_active(info, NOW))

    def test_past_reset_is_stale(self):
        info = LimitInfo("limit", NOW - timedelta(seconds=10))
        self.assertFalse(_limit_is_active(info, NOW))

    def test_unknown_reset_is_treated_as_active(self):
        info = LimitInfo("Try again later", None)
        self.assertTrue(_limit_is_active(info, NOW))

    def test_exact_reset_time_is_stale(self):
        info = LimitInfo("limit", NOW)
        self.assertFalse(_limit_is_active(info, NOW))


if __name__ == "__main__":
    unittest.main()
