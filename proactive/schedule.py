"""Pure time logic for proactive check-ins: next occurrences, quiet hours, when to stay silent."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .storage import Checkin, User

STALE_AFTER = timedelta(hours=3)        # a check-in missed by this much (bot was down) is dropped
ACTIVE_CHAT_WINDOW = timedelta(minutes=10)
MAX_UNANSWERED_FOR_FOLLOWUP = 2


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


def skip_reason(
    checkin: Checkin, user: User, now: datetime, sent_today: int, daily_budget: int
) -> str | None:
    """Return why this check-in should not fire, or None if it should."""
    if user.paused:
        return "paused"
    if now - checkin.due_at > STALE_AFTER:
        return "stale"
    if checkin.kind != "followup":
        return None  # routines are the baseline rhythm
    if sent_today >= daily_budget:
        return "daily budget reached"
    if user.unanswered_proactive >= MAX_UNANSWERED_FOR_FOLLOWUP:
        return "person is not answering"
    if user.last_user_message_at and now - user.last_user_message_at < ACTIVE_CHAT_WINDOW:
        return "conversation is active"
    return None
