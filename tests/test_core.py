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
        self.default = turn  # what to answer once `turns` run out

    usage = Usage(input_tokens=5000, output_tokens=500)  # $0.02 + $0.01 at default prices

    async def think(self, system, messages):
        self.calls.append((system, messages))
        return (self.turns.pop(0) if self.turns else self.default()), self.usage

    def why(self, call: int = -1) -> str:
        """The "Why you are called now" part of the context the model got."""
        content = self.calls[call][1][-1]["content"]
        return content.split("Why you are called now: ")[1].split("\n</context>")[0]


@pytest.fixture
def make_core():
    def factory(*turns, **settings_kw):
        settings = Settings(telegram_token="x", **settings_kw)
        return Core(settings, Storage(":memory:"), FakeLLM(*turns), build_role("mentor", settings))
    return factory


async def test_start_schedules_routines_and_onboards(make_core):
    core = make_core(turn("Привет! Задание: выпей стакан воды."))
    reply = await core.start(1, "Виктор")
    assert "Задание" in reply.text
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
    checkin = next(c for c in core.storage.pending_checkins(1) if c.kind == "morning")
    assert await core.fire(checkin, now=checkin.due_at) is None
    assert len(core.llm.calls) == 2
    assert core.storage.get_user(1).unanswered_proactive == 1  # only the greeting counts


async def test_routine_fires_and_is_rescheduled(make_core):
    core = make_core(turn("start"), turn("Доброе утро! Задание..."))
    await core.start(1, None)
    morning = next(c for c in core.storage.pending_checkins(1) if c.kind == "morning")
    reply = await core.fire(morning, now=morning.due_at + timedelta(minutes=1))
    assert reply.text.startswith("Доброе утро")
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
    assert (await core.on_message(2, None, "1")).text == "ok"
    assert (await core.on_message(2, None, "2")).text == "ok"   # $0.06 spent after this one
    assert (await core.on_message(2, None, "3")).text == DAY_LIMIT
    assert len(core.llm.calls) == 2
    # someone else is not affected
    assert (await core.on_message(3, None, "1")).text == "ok"


async def test_global_budget_alerts_once_and_spares_admins(make_core):
    core = make_core(global_daily_budget_usd=0.05, admin_user_ids={1})
    await core.on_message(2, None, "a")
    await core.on_message(3, None, "b")
    core.alerts.clear()
    assert (await core.on_message(4, None, "c")).text == GLOBAL_LIMIT
    assert (await core.on_message(5, None, "d")).text == GLOBAL_LIMIT
    assert len([a for a in core.alerts if "Общий дневной лимит" in a]) == 1
    assert (await core.on_message(1, None, "admin")).text == "ok"


async def test_rate_limit_per_minute(make_core):
    core = make_core(user_messages_per_minute=2)
    assert (await core.on_message(2, None, "1")).text == "ok"
    assert (await core.on_message(2, None, "2")).text == "ok"
    assert (await core.on_message(2, None, "3")).text == TOO_FAST


async def test_long_message_rejected_without_model(make_core):
    core = make_core(max_message_chars=10)
    assert "сократи" in (await core.on_message(2, None, "x" * 11)).text
    assert core.llm.calls == []


async def test_blocked_user_gets_no_model_and_no_checkins(make_core):
    core = make_core()
    await core.start(2, None)
    assert core.storage.set_blocked(2, True)
    assert core.storage.pending_checkins(2) == []
    assert (await core.on_message(2, None, "hi")).text == BLOCKED
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


# adapting to the person ------------------------------------------------------------------

from agent.core import BUTTONS, INITIATED_WHY, REPLY_WHY  # noqa: E402
from agent.schedule import ACTIVE, COOLING, DORMANT, LAST_CALL, cadence  # noqa: E402
from agent.signals import reply_stats, same_task, task_signal  # noqa: E402
from agent.storage import Message, Task  # noqa: E402


@pytest.fixture
def clock(monkeypatch):
    """Controls "now" for the core and the database. Starts on Mon 02.11 10:00 MSK."""

    class Clock:
        now = datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)

        def __call__(self):
            return self.now

        def advance(self, **kw):
            self.now += timedelta(**kw)

    c = Clock()
    monkeypatch.setattr("agent.storage.utcnow", c)
    monkeypatch.setattr("agent.core.utcnow", c)
    return c


def with_task():
    """Answers that give a new task each time: «Задание 1», «Задание 2»..."""
    counter = iter(range(1, 1000))

    def answer():
        title = f"Задание {next(counter)}"
        return turn(f"Сегодня: {title}", new_task=NewTask(title=title, details=""))
    return answer


def age_task(core, task_id, hours):
    """Pretend the task was given `hours` ago."""
    created = core.storage.get_task(1, task_id).created_at - timedelta(hours=hours)
    core.storage.db.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (created.isoformat(), task_id))


async def run_until(core, clock, end):
    """Fire check-ins as they come due until `end`. Returns (local time, kind, reply) for each."""
    log = []
    while True:
        pending = core.storage.pending_checkins(1)
        if not pending or pending[0].due_at > end:
            return log
        clock.now = max(clock.now, pending[0].due_at)
        for checkin in core.storage.due_checkins(clock.now):
            reply = await core.fire(checkin, now=clock.now)
            log.append((clock.now.astimezone(MSK), checkin.kind, reply))


def test_same_task_matches_restatements_only():
    assert same_task("Разобрать пенал", "Разобрать пенал.")
    assert same_task("Разобрать пенал", "Разобрать пенал сегодня")
    assert not same_task("Прогулка 10 минут", "Прогулка 5 минут")  # smaller is fine
    assert not same_task("Разобрать пенал", "Выложить из пенала одну сломанную ручку")
    assert not same_task("Позвонить маме", "Позвонить папе")
    assert not same_task("Написать план на неделю", "Написать план на день")


def test_reply_stats_counts_answers_and_own_messages():
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)

    def m(sender, minutes, proactive=False):
        return Message(sender, "", proactive, t0 + timedelta(minutes=minutes))

    stats = reply_stats([
        m("bot", 0, proactive=True), m("user", 30), m("bot", 31), m("user", 32),  # answered, then chat
        m("bot", 600, proactive=True), m("bot", 900, proactive=True),             # two ignored
        m("user", 2000), m("bot", 2001),                                          # late answer
        m("user", 2200),                                                          # on their own
    ])
    assert (stats.proactive, stats.answered, stats.initiated) == (3, 2, 1)


def test_task_signal():
    now = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)

    def t(i, status, hours_ago, title="Разобрать пенал"):
        return Task(i, title, "", status, "", now - timedelta(hours=hours_ago))

    assert task_signal([], now) is None
    assert task_signal([t(1, "open", 2)], now) is None  # today's task, no outcome yet
    one = task_signal([t(2, "open", 24), t(1, "done", 48)], now)  # yesterday's still open
    assert "last task was not done" in one and "same form" in one
    many = task_signal([t(3, "skipped", 24), t(2, "skipped", 48), t(1, "skipped", 72)], now)
    assert "last 3 tasks were not done" in many and "failed twice is dropped" in many
    assert "raise the bar" in task_signal([t(i, "done", 24 * i) for i in (1, 2, 3)], now)


def test_cadence_levels():
    assert [cadence(n) for n in range(7)] == [ACTIVE, ACTIVE, COOLING, COOLING, LAST_CALL, DORMANT, DORMANT]


async def test_repeating_a_skipped_task_is_sent_back_to_the_model(make_core, clock):
    core = make_core(turn("hi"))
    await core.start(1, None)
    for hours in (48, 24):
        task_id = core.storage.add_task(1, "Разобрать пенал", "")
        core.storage.update_task(1, task_id, "skipped", "не успела")
        age_task(core, task_id, hours)
    core.llm.turns += [
        turn("Сегодня снова пенал!", new_task=NewTask(title="Разобрать пенал", details="")),
        turn("Выложи из пенала одну сломанную ручку.", new_task=NewTask(title="Выкинуть сломанную ручку", details="")),
    ]
    morning = next(c for c in core.storage.pending_checkins(1) if c.kind == "morning")
    reply = await core.fire(morning, now=morning.due_at)
    assert reply.text == "Выложи из пенала одну сломанную ручку."
    assert len(core.llm.calls) == 3  # greeting, draft, rewrite
    assert "Task signal: The last 2 tasks were not done" in core.llm.calls[1][1][-1]["content"]
    assert "Correction from the app" in core.llm.why(2) and "2 times" in core.llm.why(2)
    titles = [t.title for t in core.storage.recent_tasks(1)]
    assert titles == ["Выкинуть сломанную ручку", "Разобрать пенал", "Разобрать пенал"]
    assert reply.task_id == core.storage.recent_tasks(1)[0].id


async def test_new_task_after_a_done_one_is_not_questioned(make_core, clock):
    core = make_core(turn("hi"))
    await core.start(1, None)
    task_id = core.storage.add_task(1, "Прогулка 10 минут", "")
    core.storage.update_task(1, task_id, "done", "")
    core.llm.turns.append(turn("Снова прогулка", new_task=NewTask(title="Прогулка 10 минут", details="")))
    await core.on_message(1, None, "что сегодня?")
    assert len(core.llm.calls) == 2


async def test_restating_todays_task_does_not_duplicate_it(make_core, clock):
    core = make_core(turn("Задание: прогулка", new_task=NewTask(title="Прогулка 10 минут", details="")))
    first = await core.start(1, None)
    core.llm.turns.append(turn("Напоминаю: прогулка", new_task=NewTask(title="Прогулка 10 минут", details="")))
    clock.advance(hours=3)
    again = await core.on_message(1, None, "какое там задание?")
    assert again.task_id == first.task_id
    assert len(core.storage.recent_tasks(1)) == 1
    assert len(core.llm.calls) == 2  # restating isn't a repeat of a missed task


async def test_silent_person_gets_fewer_messages_then_none(make_core, clock):
    core = make_core()
    core.llm.default = with_task()
    await core.start(1, None)  # Mon 10:00
    log = await run_until(core, clock, clock.now + timedelta(days=9))
    sent = [(t.strftime("%a %H:%M"), kind) for t, kind, reply in log if reply]
    assert sent == [
        ("Mon 21:30", "evening"),  # one unanswered: normal rhythm
        ("Tue 09:00", "morning"),  # two unanswered: mornings only
        ("Wed 09:00", "morning"),
        ("Sat 09:00", "morning"),  # after a pause, the last message
    ]
    assert len(core.llm.calls) == 5  # greeting + the four above
    assert "at most once a day" in core.llm.why(2) and "tiny" in core.llm.why(2)
    assert "last message before you stop writing first" in core.llm.why(-1)
    assert core.storage.get_user(1).unanswered_proactive == 5
    # One word from them and the full rhythm is back.
    clock.now = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
    await core.on_message(1, None, "я тут")
    log = await run_until(core, clock, clock.now + timedelta(days=1))
    assert [kind for _, kind, reply in log if reply] == ["evening", "morning"]


async def test_evening_is_skipped_once_the_task_is_reported(make_core, clock):
    core = make_core(turn("Задание", new_task=NewTask(title="Прогулка", details="")))
    first = await core.start(1, None)
    await core.on_button(1, None, "done", first.task_id)
    evening = next(c for c in core.storage.pending_checkins(1) if c.kind == "evening")
    calls = len(core.llm.calls)
    assert await core.fire(evening, now=evening.due_at) is None
    assert len(core.llm.calls) == calls  # no model call for it


async def test_done_button(make_core, clock):
    core = make_core(
        turn("Задание", new_task=NewTask(title="Прогулка", details=""),
             follow_ups=[FollowUp(in_minutes=120, reason="начал ли")]),
        turn("Отлично, засчитал прогулку."),
    )
    first = await core.start(1, None)
    assert first.task_id is not None
    reply = await core.on_button(1, None, "done", first.task_id)
    assert reply.text == "Отлично, засчитал прогулку." and reply.task_id is None
    assert core.storage.get_task(1, first.task_id).status == "done"
    assert core.storage.recent_messages(1, 2)[0].text == f"{BUTTONS['done']}: Прогулка"
    assert not any(c.kind == "followup" for c in core.storage.pending_checkins(1))
    assert "«✅ Готово» under task" in core.llm.why()
    # An old button for a closed task does nothing.
    assert await core.on_button(1, None, "done", first.task_id) is None
    assert await core.on_button(1, None, "swap", first.task_id) is None
    assert len(core.llm.calls) == 2


async def test_done_button_counts_a_task_the_bot_closed_as_skipped(make_core, clock):
    core = make_core(turn("Задание", new_task=NewTask(title="Прогулка", details="")))
    first = await core.start(1, None)
    core.storage.update_task(1, first.task_id, "skipped", "не отчитался")  # next morning's guess
    assert await core.on_button(1, None, "swap", first.task_id) is None
    assert (await core.on_button(1, None, "done", first.task_id)).text == "ok"
    assert core.storage.get_task(1, first.task_id).status == "done"


async def test_swap_button_gives_another_task(make_core, clock):
    core = make_core(
        turn("Задание", new_task=NewTask(title="Разобрать пенал", details="")),
        turn("Тогда так: выкинь одну сломанную ручку.", new_task=NewTask(title="Выкинуть ручку", details="")),
    )
    first = await core.start(1, None)
    reply = await core.on_button(1, None, "swap", first.task_id)
    assert core.storage.get_task(1, first.task_id).status == "skipped"
    assert reply.task_id not in (None, first.task_id)
    assert "«🔄 Поменять» under task" in core.llm.why()


async def test_own_initiative_is_noticed(make_core, clock):
    core = make_core()
    await core.start(1, None)
    await core.on_message(1, None, "привет")  # answers the greeting
    assert core.llm.why() == REPLY_WHY
    clock.advance(minutes=20)
    await core.on_message(1, None, "и ещё")  # same conversation
    assert core.llm.why() == REPLY_WHY
    clock.advance(hours=3)
    await core.on_message(1, None, "слушай, вопрос")
    assert core.llm.why() == INITIATED_WHY


async def test_resume_and_start_bring_back_the_full_rhythm(make_core, clock):
    core = make_core()
    await core.start(1, None)
    for _ in range(6):
        core.storage.add_message(1, "bot", "?", proactive=True)
    assert cadence(core.storage.get_user(1).unanswered_proactive) == DORMANT
    core.resume(1)
    assert core.storage.get_user(1).unanswered_proactive == 0
    for _ in range(6):
        core.storage.add_message(1, "bot", "?", proactive=True)
    await core.start(1, None)
    assert core.storage.get_user(1).unanswered_proactive == 1  # just the new greeting


async def test_context_tells_the_model_how_they_respond(make_core, clock):
    core = make_core()
    await core.start(1, None)
    core.storage.add_message(1, "bot", "Как дела?", proactive=True)
    core.storage.add_message(1, "bot", "Ну что?", proactive=True)
    morning = next(c for c in core.storage.pending_checkins(1) if c.kind == "morning")
    await core.fire(morning, now=morning.due_at)
    context = core.llm.calls[-1][1][-1]["content"]
    assert "answered 0 of your 3 check-ins" in context
    assert "Your last 3 messages are still unanswered" in context


async def test_engagement_report(make_core, clock):
    core = make_core()
    core.llm.default = with_task()
    await core.start(1, "Катя")
    await core.on_message(1, "Катя", "привет")
    await core.start(2, None, "silent")
    core.storage.add_message(2, "bot", "?", proactive=True)
    rows = {r.user.id: r for r in core.engagement_report(7)}
    assert (rows[1].stats.proactive, rows[1].stats.answered, rows[1].mode) == (1, 1, ACTIVE)
    assert (rows[1].done, rows[1].skipped, rows[1].open) == (0, 0, 2)
    assert (rows[2].stats.answered, rows[2].mode) == (0, COOLING)
    assert [r.user.id for r in core.engagement_report(7)] == [1, 2]
