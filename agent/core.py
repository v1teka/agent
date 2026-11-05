"""The proactive core: memory about the person + a schedule of check-ins + a model that
decides what to say, what to remember and when to write next. Roles sit on top of it."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .llm import LLM, ThinkError, Turn, Usage
from .roles import Role
from .schedule import (
    COOLING, LAST_CALL, cadence, find_zone, local_day_start, next_occurrence, push_out_of_quiet,
    skip_reason, zone_label,
)
from .signals import (
    INITIATIVE_PAUSE, ReplyStats, current, not_done, repeated_task, reply_stats, restated_task,
    same_task, task_signal,
)
from .storage import Checkin, Storage, Task, User, utcnow

log = logging.getLogger(__name__)

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
STATS_WINDOW = timedelta(days=7)
# A daily routine never comes again sooner than this, even when the person's time zone
# changes right after it ran.
ROUTINE_GAP = timedelta(hours=12)

BLOCKED = "Доступ к боту закрыт."
TOO_FAST = "Слишком много сообщений подряд. Подожди минуту, и продолжим."
DAY_LIMIT = "На сегодня лимит исчерпан, продолжим завтра."
GLOBAL_LIMIT = "Бот на сегодня устал, продолжим завтра."

# Buttons under a message with a task: callback action -> label.
BUTTONS = {"done": "✅ Готово", "swap": "🔄 Поменять"}


def can_press(action: str, task: Task | None) -> bool:
    """Whether a button still means something for this task. «Готово» also works for a task the
    bot already closed as skipped: the person did it after all."""
    if task is None or action not in BUTTONS:
        return False
    return task.status != "done" if action == "done" else task.status == "open"

CORE_RULES = """\

Technical rules:
- Messages in the history are prefixed with their time in square brackets. Never write such a
  prefix yourself. All times, there and in the context, are the person's local time.
- If memory or the conversation shows that the person lives in another time zone than the one
  in the context, or they tell you their local time and it doesn't match, set `timezone` to the
  IANA name of their zone (e.g. Asia/Novosibirsk): the app then writes to them by their clock.
  Don't ask about it just for that.
- A <context> block comes from the app, not from the person: it holds your memory about them,
  their tasks, how they respond, the current time and why you are being called now. Use it,
  don't quote it.
- Speak the person's language (Russian unless they write otherwise). Plain text, no Markdown.
- `message` is the finished text the person reads. Settle what to say before you write it and
  never correct yourself inside it ("...нет, проще —"): if you change your mind, write only the
  final version.
- At most one task per message. "Today" and "tomorrow" are relative to Now in the context.
- A message that starts with "(В ответ на ...)" is the person replying to that earlier message.
- Under a message that gives a new task the app shows two buttons, «✅ Готово» and
  «🔄 Поменять», so the person can report it or ask for another one in one tap. A message like
  "✅ Готово: ..." in the history is such a tap. You don't need to explain how to report.
- Save to new_facts anything durable you learn about the person that is not already in memory.
- When you give a task, put it in new_task as well. When the person reports on a task, update it.
- follow_ups are your own way to write first later, e.g. "check in 2 hours whether they started".
  Use them sparingly; the routine check-ins already exist.
"""

REPLY_WHY = "The person just wrote to you. Answer them."
INITIATED_WHY = (
    "The person wrote to you on their own, not in reply to a check-in. That's what you want more "
    "of: answer what they brought with full attention and don't turn it into a check-in. Don't "
    "comment on the fact that they wrote first."
)
DONE_WHY = (
    "The person pressed «✅ Готово» under task #{id} «{title}»: it's done. React in one short, warm "
    "sentence that is specific to this task. No new task now. Ask one light question only if the "
    "answer would really help you."
)
SWAP_WHY = (
    "The person pressed «🔄 Поменять» under task #{id} «{title}»: it doesn't suit them today. "
    "Give exactly one other task for today instead, clearly smaller or different, in one to three "
    "sentences, without asking them to explain."
)
COOLING_NOTE = (
    "They haven't answered your last {n} messages, so the app now writes to them at most once a "
    "day. Keep this message to one or two short sentences. If you give a task, make it tiny (under "
    "five minutes). Don't mention that they haven't answered and don't repeat earlier questions."
)
LAST_CALL_NOTE = (
    "They haven't answered your last {n} messages. This is your last message before you stop "
    "writing first. Mark any open task skipped. No new task and no question. In one or two warm "
    "sentences: you won't write first for now so as not to intrude, and any message from them, "
    "about anything, brings you back. No guilt and no summary of what they missed."
)
REPEAT_NOTE = (
    "\n\nCorrection from the app: your draft gives «{title}» again, and they didn't do it "
    "({times}, last given {when}). Write the message again with a task that is clearly smaller or "
    "different, unless the person explicitly asked for exactly this task again."
)


@dataclass(frozen=True)
class Reply:
    """What to send to the person. `task_id` is set when the message gives a task, so the
    front-end can show buttons to report it."""

    text: str
    task_id: int | None = None


@dataclass(frozen=True)
class EngagementRow:
    user: User
    stats: ReplyStats
    done: int
    skipped: int
    open: int
    mode: str  # a cadence from schedule.py, or "paused" / "blocked"


class Core:
    def __init__(self, settings: Settings, storage: Storage, llm: LLM, role: Role):
        self.settings = settings
        self.storage = storage
        self.llm = llm
        self.role = role
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.alerts: list[str] = []  # messages for admins, sent by the front-end
        self._global_alert_day: str | None = None

    # entry points ----------------------------------------------------------

    async def start(self, user_id: int, name: str | None, username: str | None = None) -> Reply | None:
        user = self._register(user_id, name, username)
        if refusal := self.refusal(user, incoming=False):
            return Reply(refusal)
        self.resume(user_id)
        async with self._locks[user_id]:
            return await self._turn(self.storage.get_user(user_id), why=self.role.onboarding, proactive=True)

    def resume(self, user_id: int) -> None:
        """The person asked for messages again: the full rhythm, however long they were silent."""
        self.storage.set_paused(user_id, False)
        self.storage.reset_unanswered(user_id)
        if user := self.storage.get_user(user_id):
            self.ensure_routines(user)

    async def on_message(
        self, user_id: int, name: str | None, text: str, username: str | None = None,
        reply_to: str | None = None,
    ) -> Reply | None:
        """`reply_to` is the text of the message the person replied to in Telegram, if any."""
        user = self._register(user_id, name, username)
        limit = self.settings.max_message_chars
        if len(text) > limit and not self.settings.is_admin(user_id):
            return Reply(f"Слишком длинное сообщение: сократи его до {limit} символов.")
        if refusal := self.refusal(user, incoming=True):
            return Reply(refusal)
        if reply_to:
            quoted = reply_to if len(reply_to) <= 300 else reply_to[:300] + "…"
            text = f"(В ответ на: «{quoted}»)\n{text}"
        async with self._locks[user_id]:
            why = INITIATED_WHY if self._initiated(self.storage.get_user(user_id)) else REPLY_WHY
            self.storage.add_message(user_id, "user", text)
            # The person spoke: earlier follow-up plans may be outdated, the model re-plans.
            self.storage.cancel_followups(user_id)
            return await self._turn(self.storage.get_user(user_id), why=why, proactive=False)

    async def on_button(
        self, user_id: int, name: str | None, action: str, task_id: int, username: str | None = None,
    ) -> Reply | None:
        """The person pressed a button under a task. None if there is nothing to say,
        e.g. the button is old and the task is already closed."""
        user = self._register(user_id, name, username)
        if user.blocked:
            return Reply(BLOCKED)
        refusal = self.refusal(user, incoming=True)
        async with self._locks[user_id]:
            task = self.storage.get_task(user_id, task_id)
            if not can_press(action, task):
                return None
            if action == "done":
                self.storage.update_task(user_id, task_id, "done", "отмечено кнопкой")
            else:
                self.storage.update_task(user_id, task_id, "skipped", "попросили поменять")
            self.storage.add_message(user_id, "user", f"{BUTTONS[action]}: {task.title}")
            self.storage.cancel_followups(user_id)
            if refusal:
                # The tap itself is saved either way; only the model's answer is limited.
                return Reply(refusal) if action == "swap" else None
            why = (DONE_WHY if action == "done" else SWAP_WHY).format(id=task.id, title=task.title)
            return await self._turn(self.storage.get_user(user_id), why=why, proactive=False)

    async def fire(self, checkin: Checkin, now: datetime | None = None) -> Reply | None:
        """Run a due check-in. Returns what to send, or None to stay silent."""
        now = now or utcnow()
        if self.storage.get_user(checkin.user_id) is None:
            self.storage.finish_checkin(checkin.id, "skipped")
            return None
        async with self._locks[checkin.user_id]:
            user = self.storage.get_user(checkin.user_id)
            tz = self.tz(user)
            sent_today = self.storage.proactive_count_since(user.id, local_day_start(now, tz))
            latest = self.storage.recent_tasks(user.id, 1)
            reason = skip_reason(
                checkin, user, now, sent_today, self.settings.daily_message_budget, tz,
                routine=None if checkin.kind == "followup" else self.role.routine(checkin.kind),
                last_proactive_at=self.storage.last_proactive_at(user.id),
                has_open_task=bool(latest) and current(latest[0], now),
            )
            if not reason and self.refusal(user, incoming=False, now=now):
                reason = "blocked or over the spending limit"
            if reason:
                log.info("Skipping check-in %s (%s) for %s: %s", checkin.id, checkin.kind, user.id, reason)
                self.storage.finish_checkin(checkin.id, "skipped")
                reply = None
            else:
                self.storage.finish_checkin(checkin.id, "sent")
                reply = await self._turn(user, why=self._why(checkin, user), proactive=True)
            if not user.blocked:
                self.ensure_routines(user, now)
            return reply

    def refusal(self, user: User, incoming: bool, now: datetime | None = None) -> str | None:
        """Why this person can't get a model answer right now, as text for them; None if they can."""
        if user.blocked:
            return BLOCKED
        s = self.settings
        if s.is_admin(user.id):
            return None
        now = now or utcnow()
        day = local_day_start(now, s.timezone)
        if self.storage.spend_since(day) >= s.global_daily_budget_usd:
            self._alert_global_limit(day)
            return GLOBAL_LIMIT
        if self.storage.spend_since(day, user.id) >= s.user_daily_budget_usd:
            return DAY_LIMIT
        if incoming:
            if self.storage.user_messages_since(user.id, now - timedelta(minutes=1)) >= s.user_messages_per_minute:
                return TOO_FAST
            if self.storage.user_messages_since(user.id, day) >= s.user_messages_per_day:
                return DAY_LIMIT
        return None

    def engagement_report(self, days: int = 7, now: datetime | None = None) -> list[EngagementRow]:
        """How each person responds over the last `days`, most active first."""
        now = now or utcnow()
        since = now - timedelta(days=days)
        rows = []
        for user in self.storage.list_users():
            stats = reply_stats(self.storage.messages_since(user.id, since))
            tasks = self.storage.tasks_since(user.id, since)
            if user.blocked:
                mode = "blocked"
            elif user.paused:
                mode = "paused"
            else:
                mode = cadence(user.unanswered_proactive)
            rows.append(EngagementRow(
                user=user,
                stats=stats,
                done=sum(t.status == "done" for t in tasks),
                skipped=sum(t.status == "skipped" for t in tasks),
                open=sum(t.status == "open" for t in tasks),
                mode=mode,
            ))
        rows.sort(key=lambda r: (r.stats.answered + r.stats.initiated, r.stats.proactive), reverse=True)
        return rows

    def cost(self, usage: Usage) -> float:
        s = self.settings
        return (
            usage.input_tokens * s.price_input
            + usage.output_tokens * s.price_output
            + usage.cache_read * s.price_cache_read
            + usage.cache_write * s.price_cache_write
        ) / 1_000_000

    def _register(self, user_id: int, name: str | None, username: str | None) -> User:
        user, is_new = self.storage.ensure_user(user_id, name, self.role.name, username)
        if is_new:
            self.ensure_routines(user)
            if not self.settings.is_admin(user_id):
                who = " ".join(p for p in (name, f"@{username}" if username else None) if p) or "без имени"
                self.alerts.append(f"Новый пользователь: {who}, id {user_id}. Заблокировать: /block {user_id}")
        return user

    def _alert_global_limit(self, day: datetime) -> None:
        key = day.date().isoformat()
        if self._global_alert_day != key:
            self._global_alert_day = key
            self.alerts.append(
                f"Общий дневной лимит ${self.settings.global_daily_budget_usd:.2f} исчерпан: "
                "до завтра бот отвечает только админам. Кто тратит: /stats"
            )

    def ensure_routines(self, user: User, now: datetime | None = None) -> None:
        now = now or utcnow()
        tz = self.tz(self.storage.get_user(user.id) or user)
        for routine in self.role.routines:
            if not self.storage.has_pending(user.id, routine.kind):
                after = now
                if last := self.storage.last_sent_at(user.id, routine.kind):
                    after = max(now, last + ROUTINE_GAP)
                due = next_occurrence(routine.at, after, tz)
                self.storage.add_checkin(user.id, due, routine.kind)

    def tz(self, user: User) -> ZoneInfo:
        """The person's time zone: the one they told the bot, or the bot's default."""
        return (find_zone(user.tz) if user.tz else None) or self.settings.timezone

    def set_timezone(self, user_id: int, tz: ZoneInfo) -> None:
        """Remember the person's time zone and move planned check-ins to their clock."""
        self.storage.set_timezone(user_id, tz.key)
        user = self.storage.get_user(user_id)
        self.storage.drop_pending_routines(user_id)
        self.ensure_routines(user)
        s = self.settings
        for checkin in self.storage.pending_checkins(user_id):
            moved = push_out_of_quiet(checkin.due_at, s.quiet_start, s.quiet_end, tz)
            if moved != checkin.due_at:
                self.storage.move_checkin(checkin.id, moved)

    # internals -------------------------------------------------------------

    def _initiated(self, user: User) -> bool:
        """Whether a message the person is writing now is their own initiative: it doesn't
        answer a check-in and comes after a pause."""
        last = self.storage.recent_messages(user.id, 1)
        return (
            bool(last)
            and user.unanswered_proactive == 0
            and utcnow() - last[-1].created_at >= INITIATIVE_PAUSE
        )

    def _why(self, checkin: Checkin, user: User) -> str:
        if checkin.kind == "followup":
            return f"Your own follow-up that you planned earlier: {checkin.reason}"
        routine = self.role.routine(checkin.kind)
        why = routine.instruction if routine else (checkin.reason or checkin.kind)
        mode = cadence(user.unanswered_proactive)
        if mode == LAST_CALL:
            return LAST_CALL_NOTE.format(n=user.unanswered_proactive)
        if mode == COOLING:
            return why + " " + COOLING_NOTE.format(n=user.unanswered_proactive)
        return why

    async def _turn(self, user: User, why: str, proactive: bool) -> Reply | None:
        system = self.role.persona + CORE_RULES
        turn = await self._think(user, system, self._messages(user, why))
        if turn.new_task and (correction := self._repeat_correction(user, turn.new_task.title)):
            log.info("User %s: the model repeated a task they didn't do, asking again", user.id)
            turn = await self._think(user, system, self._messages(user, why + correction))
        task_id = self._apply(user, turn)
        text = turn.message.strip()
        if not text:
            return None
        self.storage.add_message(user.id, "bot", text, proactive=proactive)
        return Reply(text, task_id)

    async def _think(self, user: User, system: str, messages: list[dict]) -> Turn:
        try:
            turn, usage = await self.llm.think(system, messages)
        except ThinkError as e:
            self._record(user, e.usage)
            raise
        self._record(user, usage)
        return turn

    def _repeat_correction(self, user: User, title: str) -> str | None:
        """A note for the model if `title` gives again, as is, a task the person didn't do."""
        now = utcnow()
        recent = self.storage.recent_tasks(user.id, 5)
        missed = repeated_task(title, recent, now)
        if missed is None:
            return None
        times = sum(1 for t in recent if not_done(t, now) and same_task(t.title, missed.title))
        return REPEAT_NOTE.format(
            title=missed.title,
            times="once" if times == 1 else f"{times} times",
            when=self._stamp(missed.created_at, self.tz(user)),
        )

    def _record(self, user: User, usage: Usage) -> None:
        self.storage.add_usage(
            user.id, usage.input_tokens, usage.output_tokens, usage.cache_read, usage.cache_write, self.cost(usage)
        )

    def _apply(self, user: User, turn: Turn) -> int | None:
        """Save what the model decided. Returns the id of the task the message gives, if any."""
        if turn.timezone:
            tz = find_zone(turn.timezone)
            if tz is None:
                log.warning("User %s: the model gave an unknown time zone %r", user.id, turn.timezone)
            elif user.tz is None or tz.key != user.tz:  # also when it confirms the default
                log.info("User %s: time zone %s", user.id, tz.key)
                self.set_timezone(user.id, tz)
                user = self.storage.get_user(user.id)
        for fact in turn.new_facts:
            self.storage.add_fact(user.id, fact.category, fact.text.strip())
        for update in turn.task_updates:
            self.storage.update_task(user.id, update.task_id, update.status, update.note)
        task_id = None
        if turn.new_task:
            # Restating today's task ("сегодня всё ещё: ...") shouldn't create a second copy.
            same = restated_task(turn.new_task.title, self.storage.recent_tasks(user.id, 5), utcnow())
            task_id = same.id if same else self.storage.add_task(user.id, turn.new_task.title, turn.new_task.details)
        s = self.settings
        for follow_up in turn.follow_ups[:2]:
            minutes = min(max(follow_up.in_minutes, 15), 3 * 24 * 60)
            due = push_out_of_quiet(utcnow() + timedelta(minutes=minutes), s.quiet_start, s.quiet_end, self.tz(user))
            self.storage.add_checkin(user.id, due, "followup", follow_up.reason)
        return task_id

    @staticmethod
    def _stamp(moment: datetime, tz: ZoneInfo) -> str:
        local = moment.astimezone(tz)
        return f"{WEEKDAYS_RU[local.weekday()]} {local:%d.%m %H:%M}"

    def _engagement(self, user: User, now: datetime) -> str:
        stats = reply_stats(self.storage.messages_since(user.id, now - STATS_WINDOW))
        if not stats.proactive and not stats.initiated:
            return "nothing to tell yet."
        line = (
            f"in the last 7 days they answered {stats.answered} of your {stats.proactive} check-ins "
            f"and wrote to you on their own {stats.initiated} times."
        )
        if user.unanswered_proactive >= 2:
            line += f" Your last {user.unanswered_proactive} messages are still unanswered."
        return line

    def _context(self, user: User, why: str) -> str:
        now = utcnow()
        tz = self.tz(user)
        facts = self.storage.facts(user.id)
        tasks = self.storage.recent_tasks(user.id, 7)
        pending = self.storage.pending_checkins(user.id)
        signal = task_signal(tasks, now)
        lines = [
            "<context>",
            f"Now: {self._stamp(now, tz)}, their local time ({zone_label(tz, now)}"
            + (", assumed by default)." if user.tz is None else ")."),
            f"Person: {user.name or 'unknown name'}, with you since {self._stamp(user.created_at, tz)}.",
            "",
            "Memory about the person:",
            *([f"- [{f.category}] {f.text}" for f in facts] or ["- nothing yet"]),
            "",
            "Recent tasks (newest first):",
            *(
                [f"- #{t.id} [{t.status}] {t.title} ({self._stamp(t.created_at, tz)})"
                 + (f" — {t.note}" if t.note else "") for t in tasks]
                or ["- none yet"]
            ),
            *([f"Task signal: {signal}"] if signal else []),
            "",
            f"How they respond: {self._engagement(user, now)}",
            "",
            "Already scheduled check-ins:",
            *(
                [f"- {self._stamp(c.due_at, tz)} {c.kind}" + (f": {c.reason}" if c.reason else "") for c in pending]
                or ["- none"]
            ),
            "",
            f"Why you are called now: {why}",
            "</context>",
        ]
        return "\n".join(lines)

    def _messages(self, user: User, why: str) -> list[dict]:
        history = self.storage.recent_messages(user.id, self.settings.history_limit)
        tz = self.tz(user)
        messages: list[dict] = []
        for m in history:
            role = "user" if m.sender == "user" else "assistant"
            text = f"[{self._stamp(m.created_at, tz)}] {m.text}"
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n\n" + text
            else:
                messages.append({"role": role, "content": text})
        context = self._context(user, why)
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] = context + "\n\n" + messages[-1]["content"]
        else:
            messages.append({"role": "user", "content": context})
        if messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": "(conversation started)"})
        return messages
