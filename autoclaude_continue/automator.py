"""Background worker: detect rate limits, wait, and send the chosen message.

The worker is deliberately quiet.  Polling never focuses, warms up, or launches
either app; it only reads the accessibility tree.  Focus is taken once, at send
time, and is refused while the PC is locked or the user is active.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import uiautomation as auto

from .apps import AppAdapter, adapter_for, candidates_for, target_label
from .config import Config, ConfigStore
from .input_safety import (
    focus_is_safe,
    release_modifiers,
    session_locked,
)
from .rate_limit import (
    LimitInfo,
    format_duration,
    format_reset_time,
    limit_is_active as _limit_is_active,
)
from .wake import WakeScheduler

log = logging.getLogger(__name__)

MAX_SEND_FAILURES = 3
STALE_RETRY_SECONDS = 300.0
POST_SEND_RECHECK_SECONDS = 30.0
NO_RESET_EPISODE = "no-reset"


def _episode_key(info: LimitInfo | None) -> str:
    """Identify one rate-limit episode by its reset time.

    A banner with no readable reset is treated as a single episode that lasts
    until the banner disappears.
    """

    if info is None or info.reset_at is None:
        return NO_RESET_EPISODE
    return info.reset_at.replace(microsecond=0).isoformat()


class Automator(threading.Thread):
    """Owns all UI Automation calls and the wait/send state machine."""

    def __init__(self, store: ConfigStore, events: queue.Queue) -> None:
        super().__init__(name="AutoClaudeContinue", daemon=True)
        self.store = store
        self.events = events
        self.commands: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._scheduler = WakeScheduler()
        self._scheduled_for: datetime | None = None
        self._using_wake_timer = False

        self._pending_adapter: AppAdapter | None = None
        self._pending_limit: LimitInfo | None = None
        self._send_retry_at: datetime | None = None
        self._send_failures = 0
        self._last_sent: dict[str, tuple[datetime | None, datetime]] = {}
        self._reset_retries: dict[str, int] = {}
        # App key -> episode key (the reset time of the limit banner we have
        # already answered).  This survives until the banner disappears, so a
        # slow poll can never send the same message twice for one reset.
        self._answered: dict[str, str] = {}

        self._last_launch_at = 0.0
        self._next_timeout = 1.0
        self._settle_until = 0.0
        self._manual_send = False
        # Claude developer mode hides the limit until Send is invoked.  One
        # probe per app launch makes that limit visible without polling in a
        # tight loop or resending after every restart.  The probe is re-armed
        # whenever Claude transitions from closed to open.
        self._probe_pending = True
        self._claude_open = False
        self._dev_mode_seen = bool(store.get().claude_dev_mode)

    # ------------------------------------------------------------------
    # Public API (called from the GUI thread)
    # ------------------------------------------------------------------

    def submit(self, command: str, **kwargs: Any) -> None:
        self.commands.put((command, kwargs))
        self._wake.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()
        self._scheduler.cancel()

    # ------------------------------------------------------------------
    # Thread loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        with auto.UIAutomationInitializerInThread():
            self._event_log("Monitoring started")
            while not self._stop_event.is_set():
                try:
                    self._process_commands()
                    if self._stop_event.is_set():
                        break
                    self._tick()
                except Exception as exc:
                    log.exception("Automator tick failed")
                    self._emit_state(
                        "error",
                        "Unexpected error",
                        str(exc),
                        color="red",
                    )
                    self._next_timeout = 15.0

                timeout = max(0.5, float(self._next_timeout))
                self._wake.wait(timeout)
                self._wake.clear()

            self._cancel_schedule()
            release_modifiers()
            self._emit_state("stopped", "Stopped", "Monitoring stopped.", color="gray")
            log.info("Automator stopped")

    # ------------------------------------------------------------------
    # Main state machine
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        settle = self._settle_until - time.monotonic()
        if settle > 0:
            # Give Windows a moment to restore input and the desktop after a
            # wake-from-sleep before touching UI Automation.
            self._next_timeout = max(0.5, settle)
            return

        config = self.store.get()

        # A manual "Send now" always wins, even when detection is paused; it
        # must not wait for a banner scan or a scheduled reset.
        if self._manual_send:
            self._manual_send = False
            self._probe_pending = False
            self._send_now(config)
            return

        if not config.detection_enabled:
            self._reset_pending()
            self._cancel_schedule()
            self._emit_state(
                "disabled",
                "Detection is off",
                "Turn it on to watch for rate limits and send automatically.",
                color="gray",
            )
            self._next_timeout = 3.0
            return

        now = datetime.now()

        # A due send for the app we already detected has priority over another
        # app's banner.  This keeps a real reset from being postponed because
        # the second app happens to be limited too.
        if self._pending_adapter is not None:
            adapter = self._pending_adapter
            pending = self._pending_limit
            if self._send_retry_at is not None and now < self._send_retry_at:
                self._handle_pending_send(config)
                return
            due = (
                self._scheduled_for is None
                or now >= self._scheduled_for - timedelta(seconds=5)
            )
            if not due:
                self._handle_pending_send(config)
                return

            reset_passed = (
                pending is not None
                and pending.reset_at is not None
                and pending.reset_at <= now
            )
            if reset_passed:
                # Do not scan first: right after a reset the old banner is
                # normally still on screen, and treating it as a fresh limit is
                # what caused endless five-minute retries without a send.
                log.info(
                    "Reset passed for %s; attempting the send now",
                    adapter.display_name,
                )
                window = self._window_for_send(adapter, config)
                if window is None:
                    self._emit_state(
                        "not_found",
                        f"{adapter.display_name} not found",
                        "Open the app, or enable 'Launch the app if it isn't running'.",
                        color="red",
                        app=adapter.key,
                    )
                    self._next_timeout = min(60.0, float(config.poll_seconds))
                    return
                self._send_continue(adapter, window, config)
                return

            result = self._scan_one(adapter)
            if result.snapshot is not None and result.snapshot.limit is not None:
                self._handle_limit(adapter, result.snapshot.limit, config)
                return
            if result.window is not None:
                self._send_continue(adapter, result.window, config)
                return
            if config.launch_if_missing:
                self._handle_pending_send(config)
                return
            self._emit_state(
                "not_found",
                f"{adapter.display_name} not found",
                "Open the app, or enable 'Launch the app if it isn't running'.",
                color="red",
                app=adapter.key,
            )
            self._next_timeout = min(60.0, float(config.poll_seconds))
            return

        scans = self._scan_all(config)
        self._clear_answered(scans)
        limited = [item for item in scans if item.snapshot is not None and item.snapshot.limit]

        if limited:
            self._probe_pending = False
            self._handle_limited(scans=limited, config=config)
            return

        # A due continuation wins over a fresh probe: never send a probe while
        # a reset that we already answered is waiting to be continued.
        if self._pending_adapter is not None:
            self._handle_pending_send(config)
            return

        claude_scan = next(
            (item for item in scans if item.adapter.key == "claude"),
            None,
        )
        claude_present = bool(claude_scan is not None and claude_scan.window is not None)
        if claude_present and not self._claude_open:
            # The user just opened Claude.  Check once for a developer-mode
            # limit that is invisible until Send is pressed.
            self._probe_pending = True
            log.info("Claude opened; a developer-mode probe is pending")
        self._claude_open = claude_present

        if self._probe_pending and config.claude_dev_mode:
            if config.app_target == "chatgpt":
                self._probe_pending = False
            else:
                claude = next(
                    (
                        item
                        for item in scans
                        if item.adapter.key == "claude"
                        and item.window is not None
                        and item.snapshot is not None
                        and item.snapshot.tree_ready
                        and item.snapshot.composer_present
                        and not item.snapshot.processing
                    ),
                    None,
                )
                if claude is not None:
                    self._probe_pending = False
                    self._event_log(
                        "Developer mode: sending one probe to check for a hidden limit"
                    )
                    self._send_continue(claude.adapter, claude.window, config)
                    return
                # Claude may not be open yet.  Keep the probe pending and look
                # again soon instead of waiting a full poll interval.
                self._emit_idle(scans, config)
                self._next_timeout = min(20.0, float(config.poll_seconds))
                return

        self._cancel_schedule()
        self._emit_idle(scans, config)

    def _handle_limited(
        self,
        scans: list["_ScanResult"],
        config: Config,
    ) -> None:
        # Prefer an app we have not already answered.  This matters in
        # "Automatic" mode when both apps are limited: the first app may still
        # be showing a banner we just answered, while the other is waiting.
        unhandled = [
            item
            for item in scans
            if item.adapter is not None
            and item.snapshot is not None
            and item.snapshot.limit is not None
            and not self._recently_sent(item.adapter.key, item.snapshot.limit)
        ]
        chosen = unhandled[0] if unhandled else scans[0]
        adapter = chosen.adapter
        snapshot = chosen.snapshot
        if adapter is None or snapshot is None or snapshot.limit is None:
            return

        info = snapshot.limit
        if self._recently_sent(adapter.key, info):
            self._pending_adapter = None
            self._pending_limit = None
            self._send_retry_at = None
            self._cancel_schedule()
            self._emit_state(
                "waiting",
                "Continue already sent",
                f"{adapter.display_name} is still showing the old limit banner. "
                "Waiting for it to clear.",
                reset_at=info.reset_at.isoformat() if info.reset_at else None,
                color="orange",
                app=adapter.key,
                estimated=info.estimated,
            )
            self._next_timeout = 30.0
            return

        self._handle_limit(adapter, info, config)

    def _handle_limit(
        self,
        adapter: AppAdapter,
        info: LimitInfo,
        config: Config,
    ) -> None:
        now = datetime.now()
        log.info(
            "Handling a limit for %s: reset_at=%s, estimated=%s",
            adapter.display_name,
            info.reset_at.isoformat() if info.reset_at else "unknown",
            info.estimated,
        )
        self._send_retry_at = None
        self._send_failures = 0
        self._pending_adapter = adapter
        self._pending_limit = info

        reset_at = info.reset_at
        estimated = info.estimated
        if reset_at is None:
            reset_at = now + timedelta(minutes=5)
            estimated = True

        # A parser mistake must not put the app to sleep for a week.
        if reset_at - now > timedelta(days=7):
            log.warning("Ignoring implausible reset time %s", reset_at)
            reset_at = now + timedelta(minutes=5)
            estimated = True

        retry_at = None
        if reset_at <= now:
            retries = self._reset_retries.get(adapter.key, 0)
            delay = min(STALE_RETRY_SECONDS, 10 * (2 ** min(retries, 5)))
            retry_at = now + timedelta(seconds=delay)
            estimated = True
            self._reset_retries[adapter.key] = retries + 1
            self._event_log(
                f"{adapter.display_name}'s reset time has passed; "
                f"retrying in {delay} seconds"
            )
        else:
            self._reset_retries[adapter.key] = 0

        self._schedule_wait(
            adapter,
            reset_at,
            estimated,
            config,
            retry_at=retry_at,
        )

    def _schedule_wait(
        self,
        adapter: AppAdapter,
        reset_at: datetime,
        estimated: bool,
        config: Config,
        *,
        retry_at: datetime | None = None,
    ) -> None:
        now = datetime.now()
        target = (retry_at or reset_at) + timedelta(seconds=config.wake_early_seconds)
        if target <= now:
            target = now + timedelta(seconds=2)

        same = (
            self._scheduled_for is not None
            and abs((self._scheduled_for - target).total_seconds()) < 1.0
        )
        if not same:
            used_wake = self._scheduler.schedule(target, self._on_wake_timer)
            self._scheduled_for = target
            self._using_wake_timer = used_wake
            if used_wake:
                self._event_log(
                    f"Wake timer set for {target.strftime('%Y-%m-%d %I:%M:%S %p')}"
                )
            else:
                self._event_log(
                    "Could not set a wake timer; using a normal timer "
                    "(the PC must stay awake)"
                )

        if retry_at is not None:
            detail = (
                f"{adapter.display_name}'s reset time has passed. "
                f"Retrying at {retry_at.strftime('%I:%M:%S %p')}."
            )
        else:
            detail = f"{adapter.display_name} resets {format_reset_time(reset_at, now)}."
            if estimated:
                detail += " Estimated — will re-check."
        self._emit_state(
            "waiting",
            "Waiting for rate limit reset",
            detail,
            reset_at=reset_at.isoformat(),
            color="orange",
            app=adapter.key,
            estimated=estimated,
            retry_at=retry_at.isoformat() if retry_at is not None else None,
        )
        # Safety net in case the waitable timer never signals.  The scheduler
        # normally wakes us exactly at the target time.
        self._next_timeout = max(1.0, (target - now).total_seconds() + 60.0)

    def _handle_pending_send(self, config: Config) -> None:
        adapter = self._pending_adapter
        if adapter is None:
            self._reset_pending()
            return

        now = datetime.now()
        if self._scheduled_for is not None and now < self._scheduled_for - timedelta(seconds=5):
            remaining = max(0.0, (self._scheduled_for - now).total_seconds())
            reset_at = (
                self._pending_limit.reset_at if self._pending_limit else None
            )
            stale = reset_at is not None and reset_at <= now
            if stale:
                detail = (
                    f"{adapter.display_name}'s reset time has passed. "
                    f"Retrying in {format_duration(remaining)}."
                )
            else:
                detail = f"{adapter.display_name} resets in {format_duration(remaining)}."
            self._emit_state(
                "waiting",
                "Waiting for rate limit reset",
                detail,
                reset_at=reset_at.isoformat()
                if reset_at is not None
                else self._scheduled_for.isoformat(),
                retry_at=self._scheduled_for.isoformat() if stale else None,
                color="orange",
                app=adapter.key,
                estimated=bool(self._pending_limit and self._pending_limit.estimated),
            )
            self._next_timeout = max(2.0, min(remaining, float(config.poll_seconds)))
            return

        if self._send_retry_at is not None and now < self._send_retry_at:
            remaining = (self._send_retry_at - now).total_seconds()
            self._emit_state(
                "error",
                "Could not send",
                f"{adapter.display_name} retrying in {format_duration(remaining)}.",
                reset_at=self._pending_limit.reset_at.isoformat()
                if self._pending_limit and self._pending_limit.reset_at
                else None,
                color="red",
                app=adapter.key,
            )
            self._next_timeout = min(float(config.poll_seconds), max(5.0, remaining))
            return

        window = self._window_for_send(adapter, config)
        if window is None:
            self._emit_state(
                "not_found",
                f"{adapter.display_name} not found",
                "Open the app, or enable 'Launch the app if it isn't running'.",
                color="red",
                app=adapter.key,
            )
            self._next_timeout = min(60.0, float(config.poll_seconds))
            return

        self._send_continue(adapter, window, config)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _send_now(self, config: Config) -> None:
        """Send the configured message immediately (the 'Send now' button)."""

        target = (config.app_target or "claude").strip().lower()
        if target != "auto":
            adapter = adapter_for(target)
        else:
            adapter = self._pending_adapter
            if adapter is None:
                choices = candidates_for(config)
                adapter = choices[0]
                for candidate in choices:
                    try:
                        if candidate.find_window() is not None:
                            adapter = candidate
                            break
                    except Exception:
                        log.debug(
                            "Manual send: could not find %s",
                            candidate.display_name,
                            exc_info=True,
                        )

        log.info(
            "Manual send requested via %s (chat=%r, attachments=%d)",
            adapter.display_name,
            config.chat_name or "<open chat>",
            len(config.attachments),
        )
        self._event_log(f"Manual send requested via {adapter.display_name}")

        window = self._window_for_send(adapter, config)
        if window is None:
            log.warning(
                "Manual send: %s window not found",
                adapter.display_name,
            )
            self._emit_state(
                "not_found",
                f"{adapter.display_name} not found",
                "Open the app, or enable 'Launch the app if it isn't running'.",
                color="red",
                app=adapter.key,
            )
            self._next_timeout = 30.0
            return

        self._send_continue(adapter, window, config, manual=True)

    def _send_continue(
        self,
        adapter: AppAdapter,
        window: auto.Control,
        config: Config,
        *,
        manual: bool = False,
    ) -> None:
        log.info(
            "Send attempt via %s (chat=%r, attachments=%d, manual=%s)",
            adapter.display_name,
            config.chat_name or "<open chat>",
            len(config.attachments),
            manual,
        )
        safe, reason = focus_is_safe(
            pause_when_locked=config.pause_when_locked,
            defer_when_active=config.defer_when_active,
            active_grace_seconds=config.active_grace_seconds,
        )
        if not safe:
            self._emit_state(
                "deferred",
                f"Waiting to send to {adapter.display_name}",
                f"{reason}. Will try again when it is safe.",
                reset_at=self._pending_limit.reset_at.isoformat()
                if self._pending_limit and self._pending_limit.reset_at
                else None,
                color="orange",
                app=adapter.key,
            )
            self._next_timeout = 20.0
            return

        self._emit_state(
            "sending",
            f"Sending to {adapter.display_name}",
            "Manual send requested." if manual else "The limit appears to be over.",
            color="blue",
            app=adapter.key,
        )
        focus_state = None
        try:
            focus_state = adapter.module.focus_window(window)
            # A stale upgrade modal can keep showing the old reset text after
            # the limit has actually lifted.  Clear it before judging the
            # window, or the worker would wait forever.
            adapter.dismiss_modal(window, timeout=1.5)
            snapshot = adapter.scan_window(window)

            limit = snapshot.limit
            if limit is not None and _limit_is_active(limit):
                if manual:
                    log.info(
                        "Manual send: ignoring the active rate-limit banner on %s",
                        adapter.display_name,
                    )
                else:
                    log.info(
                        "Limit still active before sending to %s",
                        adapter.display_name,
                    )
                    self._handle_limit(adapter, limit, config)
                    return
            elif limit is not None:
                log.info(
                    "Ignoring past-due limit banner before sending to %s",
                    adapter.display_name,
                )

            if snapshot.processing:
                self._defer_while_processing(adapter, config)
                return

            if config.chat_name:
                ok, message = adapter.select_chat(window, config.chat_name)
                if not ok:
                    self._send_failed(adapter, message, config)
                    return
                self._event_log(message)
                adapter.dismiss_modal(window)
                snapshot = adapter.scan_window(window)
                if snapshot.limit is not None and _limit_is_active(snapshot.limit):
                    if manual:
                        log.info(
                            "Manual send: limit banner reappeared after selecting "
                            "the chat; attempting the send anyway"
                        )
                    else:
                        self._handle_limit(adapter, snapshot.limit, config)
                        return

            result = adapter.send_message(
                window,
                config.message,
                attachments=config.attachments,
                preserve_draft=config.preserve_drafts,
            )
            if result.ok:
                self._mark_answered(adapter, window)
                self._last_sent[adapter.key] = (
                    self._pending_limit.reset_at if self._pending_limit else None,
                    datetime.now(),
                )
                self._reset_retries[adapter.key] = 0
                where = (
                    f' in "{config.chat_name}"'
                    if config.chat_name
                    else " in the open chat"
                )
                self._event_log(f'Sent "{config.message}"{where} via {adapter.display_name}')
                if result.submitted:
                    self._event_log(
                        f"{adapter.display_name} accepted the message; the UI did "
                        "not confirm it, so it will not be retried."
                    )
                try:
                    self._reset_draft_after_send(config)
                except Exception:
                    # Never let post-send bookkeeping turn a delivered message
                    # into a "failed" send that gets retried.
                    log.exception("Could not save the preset or reset the draft")
                    self._event_log(
                        "Message sent, but the draft could not be reset; see the log."
                    )
                self._reset_pending()
                self._cancel_schedule()
                self._emit_state(
                    "sent",
                    "Message sent",
                    f'Sent "{config.message}"{where} via {adapter.display_name}.',
                    color="green",
                    app=adapter.key,
                )
                # Developer mode can show a new limit only after a send.  Check
                # again soon instead of disappearing for the full poll interval.
                self._next_timeout = min(
                    POST_SEND_RECHECK_SECONDS,
                    float(config.poll_seconds),
                )
                return

            if result.processing:
                self._defer_while_processing(adapter, config)
                return

            if result.rate_limited:
                log.info("Still rate limited while sending: %s", result.message)
                if (
                    adapter.key == "claude"
                    and result.keep_composer
                    and not self.store.get().claude_dev_mode
                ):
                    self.store.update(claude_dev_mode=True)
                    self.events.put({"type": "dev_mode", "enabled": True})
                    self._event_log(
                        "Claude developer mode detected; enabled the developer-mode option."
                    )
            # A side pop can appear just after submit even when the adapter only
            # saw a generic failure.  Trust an active limit before retrying.
            snapshot = adapter.scan_window(window)
            if snapshot.limit is not None and _limit_is_active(snapshot.limit):
                self._handle_limit(adapter, snapshot.limit, config)
                return
            self._send_failed(adapter, result.message, config)
        except Exception as exc:
            log.exception("Send failed")
            self._send_failed(adapter, f"Unexpected error: {exc}", config)
        finally:
            if focus_state is not None:
                adapter.module.restore_focus(focus_state)
            release_modifiers()

    def _defer_while_processing(
        self,
        adapter: AppAdapter,
        config: Config,
    ) -> None:
        """Refuse to send while a response is streaming.

        Clicking Send at that moment becomes a click on Stop and cancels the
        answer Claude is already writing.
        """

        log.info(
            "%s is still responding; deferring the send",
            adapter.display_name,
        )
        self._emit_state(
            "waiting",
            f"Waiting for {adapter.display_name}",
            "A response is still streaming, so no message was sent. "
            "Sending now would interrupt it.",
            reset_at=self._pending_limit.reset_at.isoformat()
            if self._pending_limit and self._pending_limit.reset_at
            else None,
            color="orange",
            app=adapter.key,
        )
        self._next_timeout = 45.0

    def _mark_answered(self, adapter: AppAdapter, window: auto.Control) -> None:
        """Remember which limit episode this send answered.

        The banner usually lingers after the reset.  Keying the guard by the
        reset time (and keeping it until the banner clears) means a poll an
        hour later cannot send the same message again.
        """

        key: str | None = None
        if self._pending_limit is not None:
            # The reset we deliberately waited for.  Do not adopt a brand-new
            # future banner here: that reset still needs its own send.
            key = _episode_key(self._pending_limit)
        else:
            # A manual send.  Only a stale banner is treated as answered; a
            # fresh future banner is a new episode that must still be handled.
            try:
                snapshot = adapter.scan_window(window)
                limit = snapshot.limit if snapshot is not None else None
                if limit is not None and not _limit_is_active(limit):
                    key = _episode_key(limit)
            except Exception:
                log.debug(
                    "Could not read the limit banner after sending to %s",
                    adapter.display_name,
                    exc_info=True,
                )
        self._answered[adapter.key] = key or NO_RESET_EPISODE

    def _clear_answered(self, scans: list["_ScanResult"]) -> None:
        for item in scans:
            if item.snapshot is not None and item.snapshot.limit is None:
                self._answered.pop(item.adapter.key, None)

    def _reset_draft_after_send(self, config: Config) -> None:
        """Save a sent custom message as a preset and restore ``continue``.

        This is deliberately tied to a successful send, not to typing, so
        ordinary drafts never pollute the preset list.
        """

        message = (config.message or "").strip() or "continue"
        if message == "continue" and not config.attachments:
            return
        saved = tuple(self.store.get().saved_messages)
        if message not in saved:
            saved = saved + (message,)
        updated = self.store.update(
            message="continue",
            attachments=(),
            saved_messages=saved,
        )
        if message == "continue":
            self._event_log('Cleared the sent images; the draft is back to "continue"')
        else:
            self._event_log(
                'Saved the sent message as a preset; the draft is back to "continue"'
            )
        self.events.put(
            {
                "type": "draft_reset",
                "message": updated.message,
                "saved_messages": list(updated.saved_messages),
                "attachments": list(updated.attachments),
            }
        )

    def _send_failed(
        self,
        adapter: AppAdapter,
        message: str,
        config: Config,
    ) -> None:
        self._send_failures += 1
        log.warning(
            "Send attempt %d/%d to %s failed: %s",
            self._send_failures,
            MAX_SEND_FAILURES,
            adapter.display_name,
            message,
        )
        stale_reset = bool(
            self._pending_limit
            and self._pending_limit.reset_at is not None
            and self._pending_limit.reset_at <= datetime.now()
        )
        if self._send_failures >= MAX_SEND_FAILURES:
            self._send_failures = 0
            self._send_retry_at = None
            self._event_log(
                f"Gave up sending to {adapter.display_name} after "
                f"{MAX_SEND_FAILURES} attempts: {message}"
            )
            self._emit_state(
                "error",
                "Could not send",
                f"{message} (gave up after {MAX_SEND_FAILURES} attempts)",
                color="red",
                app=adapter.key,
            )
            self._reset_pending()
            self._cancel_schedule()
            # A past-due reset means the next scan can retry soon; do not
            # disappear for a full polling hour after a stale-banner failure.
            self._next_timeout = (
                STALE_RETRY_SECONDS if stale_reset else float(config.poll_seconds)
            )
            return

        delay = min(300, 30 * (2 ** (self._send_failures - 1)))
        self._send_retry_at = datetime.now() + timedelta(seconds=delay)
        self._emit_state(
            "error",
            "Could not send",
            f"{message} Retrying in {format_duration(delay)}.",
            color="red",
            app=adapter.key,
        )
        self._next_timeout = float(delay)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan_all(self, config: Config) -> list["_ScanResult"]:
        results: list[_ScanResult] = []
        for adapter in candidates_for(config):
            results.append(self._scan_one(adapter))
        return results

    def _scan_one(self, adapter: AppAdapter) -> "_ScanResult":
        try:
            window = adapter.find_window()
        except Exception:
            log.debug("Could not find %s", adapter.display_name, exc_info=True)
            return _ScanResult(adapter, None, None)
        if window is None:
            return _ScanResult(adapter, None, None)

        try:
            snapshot = adapter.scan_window(window)
        except Exception:
            log.debug("Could not scan %s", adapter.display_name, exc_info=True)
            return _ScanResult(adapter, window, None)

        # The tree can be blank until the app has been focused once.  Do not
        # warm it up here: polling must never steal focus.
        return _ScanResult(adapter, window, snapshot)

    def _emit_idle(self, scans: list["_ScanResult"], config: Config) -> None:
        found = [item for item in scans if item.window is not None and item.snapshot is not None]
        broken = [item for item in scans if item.window is not None and item.snapshot is None]
        if found:
            names = ", ".join(item.adapter.display_name for item in found)
            ready = [item for item in found if item.snapshot and item.snapshot.tree_ready]
            if not ready:
                self._emit_state(
                    "not_ready",
                    f"{names} is not readable yet",
                    "Open the app and focus it once so Windows can expose its UI.",
                    color="red",
                    app=found[0].adapter.key,
                )
                self._next_timeout = float(config.poll_seconds)
                return
            titles = [
                f'{item.adapter.display_name}: {item.snapshot.chat_title}'
                for item in ready
                if item.snapshot and item.snapshot.chat_title
            ]
            detail = "No active rate limit."
            if titles:
                detail = "No active rate limit. " + " · ".join(titles)
            self._emit_state(
                "idle",
                f"Monitoring {names}",
                detail,
                color="green",
                app=ready[0].adapter.key,
            )
            self._next_timeout = float(config.poll_seconds)
            return

        if broken:
            self._emit_state(
                "not_ready",
                f"{broken[0].adapter.display_name} is not readable",
                "Open the app and focus it once so Windows can expose its UI.",
                color="red",
                app=broken[0].adapter.key,
            )
            self._next_timeout = float(config.poll_seconds)
            return

        targets = ", ".join(adapter.display_name for adapter in candidates_for(config))
        self._emit_state(
            "not_found",
            f"{targets} not found",
            "Open the app, or enable 'Launch the app if it isn't running'.",
            color="red",
            app=config.app_target if config.app_target != "auto" else None,
        )
        self._next_timeout = float(config.poll_seconds)

    # ------------------------------------------------------------------
    # Window / launch helpers
    # ------------------------------------------------------------------

    def _window_for_send(
        self,
        adapter: AppAdapter,
        config: Config,
    ) -> auto.Control | None:
        window = adapter.find_window()
        if window is not None:
            return window

        if not config.launch_if_missing:
            return None

        now = time.monotonic()
        if now - self._last_launch_at < 60.0:
            return None
        self._last_launch_at = now
        self._event_log(f"Launching {adapter.display_name}")
        if not adapter.launch(config):
            return None

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self._stop_event.wait(1.0):
                return None
            window = adapter.find_window()
            if window is not None:
                return window
        self._event_log(f"{adapter.display_name} did not open in time")
        return None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _process_commands(self) -> None:
        while True:
            try:
                command, kwargs = self.commands.get_nowait()
            except queue.Empty:
                return

            if command == "stop":
                self._stop_event.set()
                return
            if command == "refresh_chats":
                self._refresh_chats()
            elif command == "check_now":
                self._send_retry_at = None
                self._send_failures = 0
                self._manual_send = True
                log.info("Manual send requested from the UI")
                self._event_log("Manual send requested")
                self._next_timeout = 0.5
            elif command == "config_changed":
                log.info("Configuration changed")
                config = self.store.get()
                if config.claude_dev_mode and not getattr(
                    self, "_dev_mode_seen", False
                ):
                    self._probe_pending = True
                    log.info("Claude developer mode enabled; probe re-armed")
                self._dev_mode_seen = bool(config.claude_dev_mode)
                self._next_timeout = min(self._next_timeout, 2.0)
            else:
                log.debug("Unknown command %r", command)

    def _refresh_chats(self) -> None:
        config = self.store.get()
        for adapter in candidates_for(config):
            try:
                window = adapter.find_window()
                if window is None:
                    continue
                if not adapter.has_tree(window):
                    continue
                chats = adapter.list_sidebar_chats(window)
                self.events.put(
                    {
                        "type": "chats",
                        "chats": chats,
                        "app": adapter.key,
                        "app_label": adapter.display_name,
                    }
                )
                return
            except Exception:
                log.exception("Could not list %s chats", adapter.display_name)
        self.events.put(
            {
                "type": "chats",
                "chats": [],
                "app": None,
                "app_label": "",
            }
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _emit_state(
        self,
        state: str,
        title: str,
        detail: str,
        reset_at: str | None = None,
        color: str = "gray",
        estimated: bool = False,
        app: str | None = None,
        retry_at: str | None = None,
    ) -> None:
        self.events.put(
            {
                "type": "state",
                "state": state,
                "title": title,
                "detail": detail,
                "reset_at": reset_at,
                "retry_at": retry_at,
                "color": color,
                "estimated": estimated,
                "app": app,
                "app_label": target_label(app) if app else "",
            }
        )

    def _event_log(self, message: str) -> None:
        log.info(message)
        self.events.put({"type": "log", "message": message, "level": "info"})

    def _recently_sent(self, app_key: str, info: LimitInfo) -> bool:
        # Persistent episode guard: once this reset has been answered, never
        # answer it again until the banner actually disappears.  The old
        # time-based guard expired before the next hourly poll, which could
        # resend the same message while Claude was still working on it.
        return self._answered.get(app_key) == _episode_key(info)

    def _on_wake_timer(self) -> None:
        self._settle_until = time.monotonic() + 5.0
        log.info("Wake timer fired; settling for 5 seconds")
        self._wake.set()

    def _reset_pending(self) -> None:
        self._pending_adapter = None
        self._pending_limit = None
        self._send_retry_at = None
        self._send_failures = 0

    def _cancel_schedule(self) -> None:
        self._scheduler.cancel()
        self._scheduled_for = None
        self._using_wake_timer = False


class _ScanResult:
    __slots__ = ("adapter", "window", "snapshot")

    def __init__(self, adapter: AppAdapter, window, snapshot) -> None:
        self.adapter = adapter
        self.window = window
        self.snapshot = snapshot
