"""Parse Claude Desktop rate-limit messages into reset times.

Claude Desktop shows a few different strings depending on the limit:

* ``You are out of free messages until tomorrow at 2:30 AM Upgrade``
* ``You hit your 5-hour message limit. It resets at 2:30 AM, or you can
  upgrade for higher limits.``
* ``Your free messages return at 2:30 AM, or keep going now with Pro.``
* ``Try again in 30 minutes`` / ``available again Monday at 9:00 AM``

This module deliberately has no UI Automation or Windows dependencies so it can
be unit-tested on any machine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta


@dataclass(frozen=True)
class LimitInfo:
    """A detected rate limit and (when possible) the time it ends."""

    raw: str
    reset_at: datetime | None
    estimated: bool = False

    @property
    def has_reset_time(self) -> bool:
        return self.reset_at is not None


def limit_is_active(info: LimitInfo, now: datetime | None = None) -> bool:
    """True while a detected limit still blocks sending.

    A reset time in the past means the banner is stale — Claude and ChatGPT
    often keep showing it after a resume from sleep or after the limit has
    already lifted.  A send must be attempted anyway; the adapter reports a
    real rate limit if one remains.
    """

    if info.reset_at is None:
        return True
    return info.reset_at > (now or datetime.now())


# These are intentionally specific.  The UI contains chat transcripts too, and
# generic words such as "limit" or "resets at" appear in normal conversation.
_LIMIT_PATTERNS = (
    re.compile(r"\bout of free messages\b"),
    re.compile(r"\bout of messages\b"),
    re.compile(r"\bfree messages return\b"),
    re.compile(r"\bmessage limit\b"),
    re.compile(r"\busage limit\b"),
    re.compile(r"\breached your (?:free )?(?:message |usage )?.{0,40}limit\b"),
    re.compile(r"\breached the .{0,40}limit\b"),
    re.compile(r"\blimit reached\b"),
    re.compile(r"\byou hit your .{0,40}limit\b"),
    re.compile(r"\byou(?:'ve| have) hit (?:the|your) .{0,40}limit\b"),
    re.compile(r"\blimit for claude messages\b"),
    re.compile(r"\brate limit exceeded\b"),
    re.compile(r"\brate limited\b"),
    re.compile(r"\bplease wait before trying again\b"),
    re.compile(r"\bwait before trying again\b"),
    re.compile(r"\bkeep chatting with claude pro\b"),
    re.compile(r"\bupgrade to pro for\b"),
    # ChatGPT / Codex desktop
    re.compile(r"\bfree plan limit\b"),
    re.compile(r"\bplus plan limit\b"),
    re.compile(r"\bmessage cap\b"),
    re.compile(r"\busage cap\b"),
    re.compile(r"\bmessage quota\b"),
    re.compile(r"\bdaily limit\b"),
    # Keep this specific: normal chat text such as "The rate limit resets at
    # 2:30 AM" must not be mistaken for a banner.
    re.compile(r"\byour limit (?:will )?resets?\b"),
    re.compile(r"\blimits? will reset\b"),
    re.compile(r"\blimit resets? (?:in|tomorrow|tonight)\b"),
    re.compile(r"\byou can send messages again\b"),
    re.compile(r"\bavailable again (?:at|in|on|tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"),
    re.compile(r"\bcome back (?:at|in|on|tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"),
    re.compile(r"\bupgrade to plus\b"),
)

_WHITESPACE_RE = re.compile(r"\s+")
_AMPM_DOT_RE = re.compile(r"\b([ap])\.?\s*m\.?\b")
_REL_TRIGGER_RE = re.compile(r"\b(?:in|after|within)\s+([^.;,]{1,80})")
_HALF_RE = re.compile(r"\bhalf\s+(?:an?\s+)?(hour|hr|minute|min)\b")
_DURATION_TOKEN_RE = re.compile(
    r"(\d+(?:\.\d+)?|an?|one)\s*"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|s|m|h|d)\b"
)

_TIME_AMPM_RE = re.compile(r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b")
_TIME_KEYWORD_RE = re.compile(
    r"\b(?:at|until|by|around|again|return|returns|reset|resets|available|back)\s+"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b"
)

_WEEKDAY_RE = re.compile(
    r"\b(?:(next|this)\s+)?"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
)
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MONTH_PATTERN = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?"
)
_MONTH_DAY_RE = re.compile(
    rf"\b({_MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
    r"(?:,?\s*(\d{4}))?\b"
)
_DAY_MONTH_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_PATTERN})"
    r"(?:,?\s*(\d{4}))?\b"
)
_DAY_ONLY_RE = re.compile(r"\bon\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\b")

_DURATION_UNITS = {
    "second": 1,
    "seconds": 1,
    "sec": 1,
    "secs": 1,
    "s": 1,
    "minute": 60,
    "minutes": 60,
    "min": 60,
    "mins": 60,
    "m": 60,
    "hour": 3600,
    "hours": 3600,
    "hr": 3600,
    "hrs": 3600,
    "h": 3600,
    "day": 86400,
    "days": 86400,
    "d": 86400,
}

# A reset time that has just passed is kept in the past so the worker retries
# immediately instead of scheduling the same clock time a day later.  This
# matters at the exact reset moment: the banner can linger for a few seconds
# while it is being refreshed.
_RECENT_RESET_GRACE = timedelta(hours=2)


def looks_like_limit(text: str) -> bool:
    """Return True when *text* looks like one of Claude's limit messages."""

    low = _normalize(text)
    return any(pattern.search(low) for pattern in _LIMIT_PATTERNS)


def parse_reset_time(text: str, now: datetime | None = None) -> datetime | None:
    """Parse the reset time from *text*.

    The returned datetime is a naive local datetime.  ``None`` means the text
    did not contain a time we understand; callers should fall back to polling.
    """

    if not text:
        return None
    now = now or datetime.now()
    low = _normalize(text)

    relative = _parse_relative(low, now)
    if relative is not None:
        return relative

    clock = _find_clock(low)
    if clock is not None:
        hour, minute, ampm, time_pos = clock
        context = _find_date_context(low, now)
        dt = _build_datetime(now, hour, minute, ampm, context)
        if dt is not None:
            return dt

    # Explicit "available now" / "back now" style messages.
    if re.search(
        r"\b(?:available|back|reset|resets|return|returns|try again)\s+"
        r"(?:again\s+)?now\b",
        low,
    ):
        return now

    return None


def limit_info(text: str, now: datetime | None = None) -> LimitInfo | None:
    """Return a :class:`LimitInfo` when *text* is a limit message."""

    if not looks_like_limit(text):
        return None
    reset = parse_reset_time(text, now)
    return LimitInfo(raw=text, reset_at=reset, estimated=reset is None)


def format_reset_time(dt: datetime, now: datetime | None = None) -> str:
    """Human-readable reset time for the GUI/log."""

    now = now or datetime.now()
    if dt.date() == now.date():
        prefix = "today"
    elif dt.date() == now.date() + timedelta(days=1):
        prefix = "tomorrow"
    else:
        prefix = dt.strftime("%a, %b %d")
    return f"{prefix} at {dt.strftime('%I:%M %p').lstrip('0')}"


def format_duration(seconds: float) -> str:
    """Format a number of seconds as a compact duration."""

    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    text = (
        text.replace("\u00a0", " ")
        .replace("\u202f", " ")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    text = _WHITESPACE_RE.sub(" ", text).strip().lower()
    text = _AMPM_DOT_RE.sub(lambda m: m.group(1) + "m", text)
    return text


def _parse_relative(text: str, now: datetime) -> datetime | None:
    trigger = _REL_TRIGGER_RE.search(text)
    if not trigger:
        return None
    chunk = trigger.group(1)

    half = _HALF_RE.search(chunk)
    if half:
        unit = half.group(1)
        seconds = 1800 if unit.startswith("h") else 30
        return now + timedelta(seconds=seconds)

    total = 0.0
    found = False
    for match in _DURATION_TOKEN_RE.finditer(chunk):
        amount_text, unit = match.group(1), match.group(2)
        if amount_text in ("a", "an", "one"):
            amount = 1.0
        else:
            amount = float(amount_text)
        multiplier = _DURATION_UNITS.get(unit)
        if multiplier is None:
            continue
        total += amount * multiplier
        found = True
    if not found:
        return None
    # A duration of zero is not useful.
    if total <= 0:
        return None
    return now + timedelta(seconds=total)


def _find_clock(text: str) -> tuple[int, int, str | None, int] | None:
    """Return ``(hour, minute, ampm, position)`` for the first clock time."""

    for match in _TIME_AMPM_RE.finditer(text):
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        ampm = match.group(3)
        if 1 <= hour <= 12 and 0 <= minute <= 59:
            return hour, minute, ampm, match.start(1)

    for match in _TIME_KEYWORD_RE.finditer(text):
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        ampm = match.group(3)
        # "at 5" is too ambiguous, but "at 5 pm" and "at 5:00" are fine.
        if ampm is None and match.group(2) is None:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute, ampm, match.start(1)
    return None


def _find_date_context(text: str, now: datetime) -> "_DateContext":
    if re.search(r"\b(?:tomorrow|next day)\b", text):
        return _DateContext(kind="tomorrow", value=now.date() + timedelta(days=1))

    if re.search(r"\b(?:today|tonight|this evening|this afternoon|this morning)\b", text):
        return _DateContext(kind="today", value=now.date())

    weekday = _WEEKDAY_RE.search(text)
    if weekday:
        modifier, name = weekday.group(1), weekday.group(2)
        target = _WEEKDAYS[name]
        delta = (target - now.weekday()) % 7
        value = now.date() + timedelta(days=delta)
        return _DateContext(
            kind="weekday",
            value=value,
            next_week=modifier == "next",
            weekday=target,
        )

    month_day = _MONTH_DAY_RE.search(text)
    if month_day:
        month = _MONTHS[month_day.group(1)]
        day = int(month_day.group(2))
        year = int(month_day.group(3)) if month_day.group(3) else now.year
        value = _safe_date(year, month, day)
        if value is not None:
            if not month_day.group(3) and value < now.date() - timedelta(days=1):
                value = _safe_date(year + 1, month, day) or value
            return _DateContext(kind="date", value=value)

    day_month = _DAY_MONTH_RE.search(text)
    if day_month:
        day = int(day_month.group(1))
        month = _MONTHS[day_month.group(2)]
        year = int(day_month.group(3)) if day_month.group(3) else now.year
        value = _safe_date(year, month, day)
        if value is not None:
            if not day_month.group(3) and value < now.date() - timedelta(days=1):
                value = _safe_date(year + 1, month, day) or value
            return _DateContext(kind="date", value=value)

    day_only = _DAY_ONLY_RE.search(text)
    if day_only:
        day = int(day_only.group(1))
        year, month = now.year, now.month
        value = _safe_date(year, month, day)
        if value is None or value < now.date():
            month += 1
            if month > 12:
                month = 1
                year += 1
            value = _safe_date(year, month, day)
        if value is not None:
            return _DateContext(kind="date", value=value)

    return _DateContext(kind=None, value=None)


@dataclass
class _DateContext:
    kind: str | None
    value: date | None
    next_week: bool = False
    weekday: int | None = None


def _build_datetime(
    now: datetime,
    hour: int,
    minute: int,
    ampm: str | None,
    context: _DateContext,
) -> datetime | None:
    if ampm == "am":
        hours = [0 if hour == 12 else hour]
    elif ampm == "pm":
        hours = [12 if hour == 12 else hour + 12]
    else:
        if hour > 23:
            return None
        if hour == 0:
            hours = [0]
        elif hour > 12:
            hours = [hour]
        elif hour == 12:
            hours = [12, 0]
        else:
            hours = [hour, hour + 12]

    if context.value is not None:
        candidates = [
            datetime.combine(context.value, dtime(h, minute))
            for h in hours
        ]
        future = [dt for dt in candidates if dt > now]
        if future:
            result = min(future, key=lambda dt: dt - now)
        else:
            result = min(candidates)
    else:
        candidates = [
            datetime.combine(now.date(), dtime(h, minute)) for h in hours
        ]
        if ampm is not None:
            # The reset banner frequently stays visible for a moment after the
            # reset.  A clock time a few minutes in the past means "retry now",
            # not "wait until the same time tomorrow".
            recent = [
                dt
                for dt in candidates
                if now - _RECENT_RESET_GRACE <= dt <= now
            ]
            if recent:
                return max(recent)
        future = [dt for dt in candidates if dt > now]
        if future:
            result = min(future, key=lambda dt: dt - now)
        else:
            result = min(dt + timedelta(days=1) for dt in candidates)

    if context.kind == "weekday":
        if context.next_week and result.date() == now.date():
            result += timedelta(days=7)
        elif result <= now:
            result += timedelta(days=7)
    return result


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None
