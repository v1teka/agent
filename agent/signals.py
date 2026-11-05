"""What the app works out about the person by itself, to steer the model: how they answer
the bot, and how their tasks go. Pure functions over stored messages and tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import takewhile
from datetime import datetime, timedelta

from .storage import Message, Task

# A message counts as written "on their own" if it doesn't answer a check-in and comes
# after at least this much silence.
INITIATIVE_PAUSE = timedelta(hours=1)
# An open task this old is not done: with daily routines the next morning has come. A task
# given in the evening for tomorrow is still current at that morning.
MISSED_AFTER = timedelta(hours=20)


@dataclass(frozen=True)
class ReplyStats:
    proactive: int  # messages the bot wrote first
    answered: int   # of them, followed by a message from the person before the next one
    initiated: int  # messages the person wrote on their own


def reply_stats(messages: list[Message]) -> ReplyStats:
    """Messages must be in chronological order."""
    proactive = answered = initiated = 0
    waiting = False  # the last proactive message has no answer yet
    previous: datetime | None = None
    for m in messages:
        if m.sender == "bot":
            if m.proactive:
                proactive += 1
                waiting = True
        elif waiting:
            answered += 1
            waiting = False
        elif previous is not None and m.created_at - previous >= INITIATIVE_PAUSE:
            initiated += 1
        previous = m.created_at
    return ReplyStats(proactive, answered, initiated)


def _words(title: str) -> tuple[set[str], list[str]]:
    words = re.findall(r"\w+", title.lower().replace("ё", "е"))
    numbers = [w for w in words if w.isdigit()]
    # A five-letter prefix is a crude Russian stem: "пенал" and "пенала" match.
    stems = {w[:5] for w in words if not w.isdigit() and len(w) >= 3}
    return stems, numbers


def same_task(a: str, b: str) -> bool:
    """True when one title is the other one again, maybe with extra words.

    "Разобрать пенал" and "Разобрать пенал сегодня" are the same task; "Прогулка 5 минут"
    after "Прогулка 10 минут" is a smaller one, and "Позвонить папе" is a different one.
    """
    stems_a, numbers_a = _words(a)
    stems_b, numbers_b = _words(b)
    if not stems_a or not stems_b or numbers_a != numbers_b:
        return False
    return stems_a <= stems_b or stems_b <= stems_a


def not_done(task: Task, now: datetime) -> bool:
    """Skipped, or still open after the next morning has come."""
    return task.status == "skipped" or (task.status == "open" and now - task.created_at >= MISSED_AFTER)


def current(task: Task, now: datetime) -> bool:
    """Open and still fresh: today's task, or one given last night for today."""
    return task.status == "open" and now - task.created_at < MISSED_AFTER


def repeated_task(title: str, recent: list[Task], now: datetime) -> Task | None:
    """A recent task the person didn't do that `title` gives again in the same form."""
    for task in recent[:5]:
        if not_done(task, now) and same_task(title, task.title):
            return task
    return None


def restated_task(title: str, recent: list[Task], now: datetime) -> Task | None:
    """The current open task that `title` merely restates."""
    for task in recent[:5]:
        if current(task, now) and same_task(title, task.title):
            return task
    return None


def task_signal(tasks: list[Task], now: datetime) -> str | None:
    """A hint for the model about how hard the next task should be. Tasks are newest first."""
    outcomes = [t for t in tasks if t.status == "done" or not_done(t, now)]
    if not outcomes:
        return None
    last_done = outcomes[0].status == "done"
    streak = list(takewhile(lambda t: (t.status == "done") == last_done, outcomes))
    if last_done:
        return f"They did the last {len(streak)} tasks: you can raise the bar a little." if len(streak) >= 3 else None
    missed = streak
    titles = ", ".join(f"«{t.title}»" for t in missed[:4])
    if len(missed) == 1:
        return (
            f"The last task was not done: {titles}. Don't give it again in the same form: "
            "shrink it to a first step of 2-5 minutes, or pick something else."
        )
    return (
        f"The last {len(missed)} tasks were not done (newest first): {titles}. Don't repeat any of them; "
        "anything that failed twice is dropped for now. Give something different and tiny that "
        "is easy to start, in an area they care about. You may ask once, lightly, what got in the way."
    )
