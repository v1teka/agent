from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from proactive.config import Settings
from proactive.core import Core
from proactive.llm import FollowUp, NewFact, NewTask, TaskUpdate, Turn
from proactive.roles import build_role
from proactive.schedule import in_quiet_hours, next_occurrence, push_out_of_quiet
from proactive.storage import Storage

MSK = ZoneInfo("Europe/Moscow")


def turn(message="ok", **kw) -> Turn:
    return Turn(
        message=message,
        new_facts=kw.get("new_facts", []),
        new_task=kw.get("new_task"),
        task_updates=kw.get("task_updates", []),
        follow_ups=kw.get("follow_ups", []),
    )


class FakeLLM:
    def __init__(self, *turns: Turn):
        self.turns = list(turns)
        self.calls: list[tuple[str, list[dict]]] = []

    async def think(self, system, messages):
        self.calls.append((system, messages))
        return self.turns.pop(0) if self.turns else turn()


@pytest.fixture
def make_core():
    def factory(*turns):
        settings = Settings(telegram_token="x")
        return Core(settings, Storage(":memory:"), FakeLLM(*turns), build_role("mentor", settings))
    return factory


async def test_start_schedules_routines_and_onboards(make_core):
    core = make_core(turn("Привет! Задание: выпей стакан воды."))
    text = await core.start(1, "Виктор")
    assert "Задание" in text
    kinds = sorted(c.kind for c in core.storage.pending_checkins(1))
    assert kinds == ["evening", "morning"]
    assert core.storage.get_user(1).unanswered_proactive == 1


async def test_message_saves_facts_tasks_and_followups(make_core):
    core = make_core(turn(
        "Отлично, тогда сегодня 10 минут прогулки.",
        new_facts=[NewFact(category="work", text="Программист")],
        new_task=NewTask(title="Прогулка 10 минут", details="После обеда"),
        follow_ups=[FollowUp(in_minutes=120, reason="спросить про прогулку")],
    ))
    await core.on_message(1, "Виктор", "Я программист, сижу весь день")
    s = core.storage
    assert [f.text for f in s.facts(1)] == ["Программист"]
    assert s.recent_tasks(1)[0].title == "Прогулка 10 минут"
    assert any(c.kind == "followup" for c in s.pending_checkins(1))
    # context reached the model together with the person's message
    _, messages = core.llm.calls[0]
    assert messages[-1]["role"] == "user"
    assert "<context>" in messages[-1]["content"] and "программист" in messages[-1]["content"]


async def test_task_update_and_new_message_cancels_old_followups(make_core):
    core = make_core(
        turn(new_task=NewTask(title="t", details="d"), follow_ups=[FollowUp(in_minutes=60, reason="r")]),
    )
    await core.on_message(1, None, "hi")
    task_id = core.storage.recent_tasks(1)[0].id
    core.llm.turns.append(turn(task_updates=[TaskUpdate(task_id=task_id, status="done", note="сделал")]))
    await core.on_message(1, None, "сделал")
    assert core.storage.recent_tasks(1)[0].status == "done"
    assert not any(c.kind == "followup" for c in core.storage.pending_checkins(1))


async def test_empty_message_means_silence(make_core):
    core = make_core(turn("hi"), turn(""))
    await core.start(1, None)
    checkin = core.storage.pending_checkins(1)[0]
    assert await core.fire(checkin, now=checkin.due_at) is None
    assert len(core.llm.calls) == 2
    assert core.storage.get_user(1).unanswered_proactive == 1  # only the greeting counts


async def test_routine_fires_and_is_rescheduled(make_core):
    core = make_core(turn("start"), turn("Доброе утро! Задание..."))
    await core.start(1, None)
    morning = next(c for c in core.storage.pending_checkins(1) if c.kind == "morning")
    text = await core.fire(morning, now=morning.due_at + timedelta(minutes=1))
    assert text.startswith("Доброе утро")
    pending = [c for c in core.storage.pending_checkins(1) if c.kind == "morning"]
    assert len(pending) == 1 and pending[0].due_at > morning.due_at


async def test_followup_skipped_when_person_ignores_bot(make_core):
    core = make_core(turn("a"))
    await core.start(1, None)
    core.storage.add_message(1, "bot", "b", proactive=True)  # two unanswered now
    now = datetime.now(timezone.utc)
    cid = core.storage.add_checkin(1, now, "followup", "nudge")
    checkin = next(c for c in core.storage.pending_checkins(1) if c.id == cid)
    assert await core.fire(checkin, now=now) is None
    assert len(core.llm.calls) == 1  # model not called for the skipped follow-up


async def test_stale_checkin_is_dropped(make_core):
    core = make_core(turn("a"))
    await core.start(1, None)
    checkin = core.storage.pending_checkins(1)[0]
    assert await core.fire(checkin, now=checkin.due_at + timedelta(hours=5)) is None




def test_schedule_helpers():
    now = datetime(2026, 10, 16, 10, 0, tzinfo=MSK)
    assert next_occurrence(time(9, 0), now, MSK).day == 17
    assert next_occurrence(time(21, 30), now, MSK).day == 16
    late = datetime(2026, 10, 16, 23, 30, tzinfo=MSK)
    assert in_quiet_hours(late, time(23), time(8), MSK)
    moved = push_out_of_quiet(late, time(23), time(8), MSK)
    assert (moved.day, moved.hour) == (17, 8)
    early = datetime(2026, 10, 17, 6, 0, tzinfo=MSK)
    assert push_out_of_quiet(early, time(23), time(8), MSK).hour == 8
