from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from agent.config import Settings
from agent.core import Core
from agent.core import BLOCKED, DAY_LIMIT, GLOBAL_LIMIT, TOO_FAST
from agent.llm import FollowUp, NewFact, NewTask, TaskUpdate, Turn, Usage
from agent.roles import build_role
from agent.schedule import in_quiet_hours, next_occurrence, push_out_of_quiet
from agent.storage import Storage

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

    usage = Usage(input_tokens=5000, output_tokens=500)  # $0.02 + $0.01 at default prices

    async def think(self, system, messages):
        self.calls.append((system, messages))
        return (self.turns.pop(0) if self.turns else turn()), self.usage


@pytest.fixture
def make_core():
    def factory(*turns, **settings_kw):
        settings = Settings(telegram_token="x", **settings_kw)
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


async def test_usage_is_recorded_with_cost(make_core):
    core = make_core()
    await core.on_message(1, "Маша", "привет", "masha")
    row = core.storage.usage_report()[0]
    assert (row.user_id, row.username, row.turns, row.tokens) == (1, "masha", 1, 5500)
    assert row.cost_usd == pytest.approx(0.03)


async def test_new_user_alerts_admins_but_admin_does_not(make_core):
    core = make_core(admin_user_ids={1})
    await core.on_message(1, "Виктор", "hi")
    assert core.alerts == []
    await core.on_message(2, "Маша", "hi", "masha")
    assert core.alerts == ["Новый пользователь: Маша @masha, id 2. Заблокировать: /block 2"]


async def test_user_daily_budget(make_core):
    core = make_core(user_daily_budget_usd=0.05)
    assert await core.on_message(2, None, "1") == "ok"
    assert await core.on_message(2, None, "2") == "ok"   # $0.06 spent after this one
    assert await core.on_message(2, None, "3") == DAY_LIMIT
    assert len(core.llm.calls) == 2
    # someone else is not affected
    assert await core.on_message(3, None, "1") == "ok"


async def test_global_budget_alerts_once_and_spares_admins(make_core):
    core = make_core(global_daily_budget_usd=0.05, admin_user_ids={1})
    await core.on_message(2, None, "a")
    await core.on_message(3, None, "b")
    core.alerts.clear()
    assert await core.on_message(4, None, "c") == GLOBAL_LIMIT
    assert await core.on_message(5, None, "d") == GLOBAL_LIMIT
    assert len([a for a in core.alerts if "Общий дневной лимит" in a]) == 1
    assert await core.on_message(1, None, "admin") == "ok"


async def test_rate_limit_per_minute(make_core):
    core = make_core(user_messages_per_minute=2)
    assert await core.on_message(2, None, "1") == "ok"
    assert await core.on_message(2, None, "2") == "ok"
    assert await core.on_message(2, None, "3") == TOO_FAST


async def test_long_message_rejected_without_model(make_core):
    core = make_core(max_message_chars=10)
    assert "сократи" in await core.on_message(2, None, "x" * 11)
    assert core.llm.calls == []


async def test_blocked_user_gets_no_model_and_no_checkins(make_core):
    core = make_core()
    await core.start(2, None)
    assert core.storage.set_blocked(2, True)
    assert core.storage.pending_checkins(2) == []
    assert await core.on_message(2, None, "hi") == BLOCKED
    assert len(core.llm.calls) == 1


async def test_checkin_skipped_when_over_budget(make_core):
    core = make_core(turn("hi"), user_daily_budget_usd=0.01)
    await core.start(2, None)  # spends $0.03
    checkin = core.storage.pending_checkins(2)[0]
    assert await core.fire(checkin, now=checkin.due_at) is None
    assert len(core.llm.calls) == 1


def test_migration_adds_new_columns(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, role TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0,"
                " created_at TEXT NOT NULL, last_user_message_at TEXT, unanswered_proactive INTEGER NOT NULL DEFAULT 0)")
    con.execute("INSERT INTO users (id, name, role, created_at) VALUES (1, 'Виктор', 'mentor', '2026-10-17T00:00:00+00:00')")
    con.commit(); con.close()
    user = Storage(db).get_user(1)
    assert user.name == "Виктор" and user.blocked is False and user.username is None


def test_access_settings(monkeypatch, tmp_path):
    for k, v in {"TELEGRAM_BOT_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "ALLOWED_USER_IDS": "7"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    monkeypatch.delenv("ACCESS", raising=False)
    s = Settings.from_env(tmp_path / "none.env")
    assert s.is_admin(7) and s.public and s.may_use(99)
    monkeypatch.setenv("ACCESS", "private")
    s = Settings.from_env(tmp_path / "none.env")
    assert s.may_use(7) and not s.may_use(99)


async def test_reply_context_reaches_model(make_core):
    core = make_core()
    await core.on_message(1, None, "сделал!", reply_to="Задание: прогулка 10 минут")
    _, messages = core.llm.calls[0]
    assert "(В ответ на: «Задание: прогулка 10 минут»)\nсделал!" in messages[-1]["content"]
