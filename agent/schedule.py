"""Pure time logic for proactive check-ins: next occurrences, quiet hours, when to stay silent."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from functools import cache
from zoneinfo import ZoneInfo, available_timezones

from .roles.base import Routine
from .storage import Checkin, User

STALE_AFTER = timedelta(hours=3)        # a check-in missed by this much (bot was down) is dropped
ACTIVE_CHAT_WINDOW = timedelta(minutes=10)

# How actively the bot writes first depends on how many of its messages in a row went
# unanswered. Any message from the person brings it back to ACTIVE.
ACTIVE = "active"        # the full rhythm: all routines and the model's own follow-ups
COOLING = "cooling"      # one light check-in a day (the routine kept when quiet)
LAST_CALL = "last_call"  # after a pause, one last message: "I'll stop writing first"
DORMANT = "dormant"      # nothing until the person writes
COOLING_AFTER = 2        # unanswered messages in a row
LAST_CALL_AFTER = 4
LAST_CALL_PAUSE_DAYS = 3  # days between the last cooling message and the last call


@cache
def _zone_names() -> dict[str, str]:
    return {name.lower(): name for name in available_timezones()}


def find_zone(name: str) -> ZoneInfo | None:
    """An IANA time zone by its name, in any letter case."""
    key = _zone_names().get(name.strip().lower())
    return ZoneInfo(key) if key else None


_OFFSET = re.compile(r"^(utc|gmt|мск|msk)?([+\-−])(\d{1,2})(?::?00)?$")


def parse_timezone(text: str) -> ZoneInfo | None:
    """A zone from what a person types: «Asia/Novosibirsk», «UTC+7», «+7» or «МСК+4»."""
    text = text.strip()
    if zone := find_zone(text):
        return zone
    compact = text.lower().replace(" ", "")
    if compact in ("мск", "msk", "москва"):
        return ZoneInfo("Europe/Moscow")
    match = _OFFSET.match(compact)
    if not match:
        return None
    base, sign, number = match.groups()
    hours = (3 if base in ("мск", "msk") else 0) + (-1 if sign in "-−" else 1) * int(number)
    if not -12 <= hours <= 14:
        return None
    # Etc/GMT zones count the other way round: Etc/GMT-7 is UTC+7.
    return ZoneInfo("Etc/UTC" if hours == 0 else f"Etc/GMT{-hours:+d}")


def utc_offset(tz: ZoneInfo, moment: datetime) -> str:
    minutes = int(moment.astimezone(tz).utcoffset().total_seconds()) // 60
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{'-' if minutes < 0 else '+'}{hours}" + (f":{rest:02d}" if rest else "")


def zone_label(tz: ZoneInfo, moment: datetime) -> str:
    """«Asia/Novosibirsk, UTC+7», or just «UTC+7» for a zone made from an offset."""
    offset = utc_offset(tz, moment)
    return offset if tz.key.startswith("Etc/") else f"{tz.key}, {offset}"


def cadence(unanswered: int) -> str:
    if unanswered < COOLING_AFTER:
        return ACTIVE
    if unanswered < LAST_CALL_AFTER:
        return COOLING
    if unanswered == LAST_CALL_AFTER:
        return LAST_CALL
    return DORMANT


def next_occurrence(at: time, now: datetime, tz: ZoneInfo) -> datetime:
    local_now = now.astimezone(tz)
    candidate = datetime.combine(local_now.date(), at, tzinfo=tz)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def in_quiet_hours(moment: datetime, start: time, end: time, tz: ZoneInfo) -> bool:
    t = moment.astimezone(tz).time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def push_out_of_quiet(moment: datetime, start: time, end: time, tz: ZoneInfo) -> datetime:
    """Move a moment that falls into quiet hours to the end of them."""
    if not in_quiet_hours(moment, start, end, tz):
        return moment
    local = moment.astimezone(tz)
    candidate = datetime.combine(local.date(), end, tzinfo=tz)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


def local_day_start(now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    return datetime.combine(local.date(), time(0, 0), tzinfo=tz)


def days_between(earlier: datetime, later: datetime, tz: ZoneInfo) -> int:
    """Calendar days between two moments in local time."""
    return (later.astimezone(tz).date() - earlier.astimezone(tz).date()).days


def skip_reason(
    checkin: Checkin,
    user: User,
    now: datetime,
    sent_today: int,
    daily_budget: int,
    tz: ZoneInfo,
    routine: Routine | None = None,
    last_proactive_at: datetime | None = None,
    has_open_task: bool = False,
) -> str | None:
    """Return why this check-in should not fire, or None if it should.

    `routine` is the role's routine for this check-in, None for the model's own follow-ups.
    """
    if user.paused:
        return "paused"
    if now - checkin.due_at > STALE_AFTER:
        return "stale"
    mode = cadence(user.unanswered_proactive)
    if mode == DORMANT:
        return "person stopped answering; waiting for them to write"
    if routine is None:
        if mode != ACTIVE:
            return "person is not answering"
        if sent_today >= daily_budget:
            return "daily budget reached"
        if user.last_user_message_at and now - user.last_user_message_at < ACTIVE_CHAT_WINDOW:
            return "conversation is active"
        return None
    if routine.needs_open_task and not has_open_task:
        return "no open task to ask about"
    if mode == ACTIVE:
        return None
    if not routine.keep_when_quiet:
        return "person is not answering; only the main check-in stays"
    if (
        mode == LAST_CALL
        and last_proactive_at
        and days_between(last_proactive_at, now, tz) < LAST_CALL_PAUSE_DAYS
    ):
        return "pause before the last message"
    return None
