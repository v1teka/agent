"""The proactive core: memory about the person + a schedule of check-ins + a model that
decides what to say, what to remember and when to write next. Roles sit on top of it."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from .config import Settings
from .llm import LLM, ThinkError, Turn, Usage
from .roles import Role
from .schedule import local_day_start, next_occurrence, push_out_of_quiet, skip_reason
from .storage import Checkin, Storage, User, utcnow

log = logging.getLogger(__name__)

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

BLOCKED = "Доступ к боту закрыт."
TOO_FAST = "Слишком много сообщений подряд. Подожди минуту, и продолжим."
DAY_LIMIT = "На сегодня лимит исчерпан, продолжим завтра."
GLOBAL_LIMIT = "Бот на сегодня устал, продолжим завтра."

CORE_RULES = """\

Technical rules:
- Messages in the history are prefixed with their local time in square brackets. Never write
  such a prefix yourself.
- A <context> block comes from the app, not from the person: it holds your memory about them,
  their tasks, the current time and why you are being called now. Use it, don't quote it.
- Speak the person's language (Russian unless they write otherwise). Plain text, no Markdown.
- `message` is the finished text the person reads. Settle what to say before you write it and
  never correct yourself inside it ("...нет, проще —"): if you change your mind, write only the
  final version.
- At most one task per message. "Today" and "tomorrow" are relative to Now in the context.
- A message that starts with "(В ответ на ...)" is the person replying to that earlier message.
- Save to new_facts anything durable you learn about the person that is not already in memory.
- When you give a task, put it in new_task as well. When the person reports on a task, update it.
- follow_ups are your own way to write first later, e.g. "check in 2 hours whether they started".
  Use them sparingly; the routine check-ins already exist.
"""


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

    async def start(self, user_id: int, name: str | None, username: str | None = None) -> str | None:
        user = self._register(user_id, name, username)
        if refusal := self.refusal(user, incoming=False):
            return refusal
        self.storage.set_paused(user_id, False)
        self.ensure_routines(user)
        async with self._locks[user_id]:
            return await self._turn(user, why=self.role.onboarding, proactive=True)

    async def on_message(
        self, user_id: int, name: str | None, text: str, username: str | None = None,
        reply_to: str | None = None,
    ) -> str | None:
        """`reply_to` is the text of the message the person replied to in Telegram, if any."""
        user = self._register(user_id, name, username)
        limit = self.settings.max_message_chars
        if len(text) > limit and not self.settings.is_admin(user_id):
            return f"Слишком длинное сообщение: сократи его до {limit} символов."
        if refusal := self.refusal(user, incoming=True):
            return refusal
        if reply_to:
            quoted = reply_to if len(reply_to) <= 300 else reply_to[:300] + "…"
            text = f"(В ответ на: «{quoted}»)\n{text}"
        async with self._locks[user_id]:
            self.storage.add_message(user_id, "user", text)
            # The person spoke: earlier follow-up plans may be outdated, the model re-plans.
            self.storage.cancel_followups(user_id)
            user = self.storage.get_user(user_id)
            return await self._turn(user, why="The person just wrote to you. Answer them.", proactive=False)

    async def fire(self, checkin: Checkin, now: datetime | None = None) -> str | None:
        """Run a due check-in. Returns the text to send, or None to stay silent."""
        now = now or utcnow()
        user = self.storage.get_user(checkin.user_id)
        if user is None:
            self.storage.finish_checkin(checkin.id, "skipped")
            return None
        async with self._locks[user.id]:
            sent_today = self.storage.proactive_count_since(
                user.id, local_day_start(now, self.settings.timezone)
            )
            reason = skip_reason(checkin, user, now, sent_today, self.settings.daily_message_budget)
            if not reason and self.refusal(user, incoming=False, now=now):
                reason = "blocked or over the spending limit"
            if reason:
                log.info("Skipping check-in %s (%s) for %s: %s", checkin.id, checkin.kind, user.id, reason)
                self.storage.finish_checkin(checkin.id, "skipped")
                text = None
            else:
                self.storage.finish_checkin(checkin.id, "sent")
                text = await self._turn(user, why=self._why(checkin), proactive=True)
            if not user.blocked:
                self.ensure_routines(user, now)
            return text

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
        for routine in self.role.routines:
            if not self.storage.has_pending(user.id, routine.kind):
                due = next_occurrence(routine.at, now, self.settings.timezone)
                self.storage.add_checkin(user.id, due, routine.kind)

    # internals -------------------------------------------------------------

    def _why(self, checkin: Checkin) -> str:
        if checkin.kind == "followup":
            return f"Your own follow-up that you planned earlier: {checkin.reason}"
        for routine in self.role.routines:
            if routine.kind == checkin.kind:
                return routine.instruction
        return checkin.reason or checkin.kind

    async def _turn(self, user: User, why: str, proactive: bool) -> str | None:
        system = self.role.persona + CORE_RULES
        messages = self._messages(user, why)
        try:
            turn, usage = await self.llm.think(system, messages)
        except ThinkError as e:
            self._record(user, e.usage)
            raise
        self._record(user, usage)
        self._apply(user, turn)
        text = turn.message.strip()
        if not text:
            return None
        self.storage.add_message(user.id, "bot", text, proactive=proactive)
        return text

    def _record(self, user: User, usage: Usage) -> None:
        self.storage.add_usage(
            user.id, usage.input_tokens, usage.output_tokens, usage.cache_read, usage.cache_write, self.cost(usage)
        )

    def _apply(self, user: User, turn: Turn) -> None:
        for fact in turn.new_facts:
            self.storage.add_fact(user.id, fact.category, fact.text.strip())
        for update in turn.task_updates:
            self.storage.update_task(user.id, update.task_id, update.status, update.note)
        if turn.new_task:
            self.storage.add_task(user.id, turn.new_task.title, turn.new_task.details)
        s = self.settings
        for follow_up in turn.follow_ups[:2]:
            minutes = min(max(follow_up.in_minutes, 15), 3 * 24 * 60)
            due = push_out_of_quiet(utcnow() + timedelta(minutes=minutes), s.quiet_start, s.quiet_end, s.timezone)
            self.storage.add_checkin(user.id, due, "followup", follow_up.reason)

    def _stamp(self, moment: datetime) -> str:
        local = moment.astimezone(self.settings.timezone)
        return f"{WEEKDAYS_RU[local.weekday()]} {local:%d.%m %H:%M}"

    def _context(self, user: User, why: str) -> str:
        facts = self.storage.facts(user.id)
        tasks = self.storage.recent_tasks(user.id, 7)
        pending = self.storage.pending_checkins(user.id)
        now = utcnow()
        lines = [
            "<context>",
            f"Now: {self._stamp(now)} ({self.settings.timezone.key}).",
            f"Person: {user.name or 'unknown name'}, with you since {self._stamp(user.created_at)}.",
            "",
            "Memory about the person:",
            *([f"- [{f.category}] {f.text}" for f in facts] or ["- nothing yet"]),
            "",
            "Recent tasks (newest first):",
            *(
                [f"- #{t.id} [{t.status}] {t.title} ({self._stamp(t.created_at)})"
                 + (f" — {t.note}" if t.note else "") for t in tasks]
                or ["- none yet"]
            ),
            "",
            "Already scheduled check-ins:",
            *(
                [f"- {self._stamp(c.due_at)} {c.kind}" + (f": {c.reason}" if c.reason else "") for c in pending]
                or ["- none"]
            ),
            "",
            f"Why you are called now: {why}",
            "</context>",
        ]
        return "\n".join(lines)

    def _messages(self, user: User, why: str) -> list[dict]:
        history = self.storage.recent_messages(user.id, self.settings.history_limit)
        messages: list[dict] = []
        for m in history:
            role = "user" if m.sender == "user" else "assistant"
            text = f"[{self._stamp(m.created_at)}] {m.text}"
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
