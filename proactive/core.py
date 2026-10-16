"""The proactive core: memory about the person + a schedule of check-ins + a model that
decides what to say, what to remember and when to write next. Roles sit on top of it."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from .config import Settings
from .llm import LLM, Turn
from .roles import Role
from .schedule import local_day_start, next_occurrence, push_out_of_quiet, skip_reason
from .storage import Checkin, Storage, User, utcnow

log = logging.getLogger(__name__)

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

CORE_RULES = """\

Technical rules:
- Messages in the history are prefixed with their local time in square brackets. Never write
  such a prefix yourself.
- A <context> block comes from the app, not from the person: it holds your memory about them,
  their tasks, the current time and why you are being called now. Use it, don't quote it.
- Speak the person's language (Russian unless they write otherwise). Plain text, no Markdown.
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

    # entry points ----------------------------------------------------------

    async def start(self, user_id: int, name: str | None) -> str | None:
        user, _ = self.storage.ensure_user(user_id, name, self.role.name)
        self.storage.set_paused(user_id, False)
        self.ensure_routines(user)
        async with self._locks[user_id]:
            return await self._turn(user, why=self.role.onboarding, proactive=True)

    async def on_message(self, user_id: int, name: str | None, text: str) -> str | None:
        user, is_new = self.storage.ensure_user(user_id, name, self.role.name)
        if is_new:
            self.ensure_routines(user)
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
            if reason:
                log.info("Skipping check-in %s (%s) for %s: %s", checkin.id, checkin.kind, user.id, reason)
                self.storage.finish_checkin(checkin.id, "skipped")
                text = None
            else:
                self.storage.finish_checkin(checkin.id, "sent")
                text = await self._turn(user, why=self._why(checkin), proactive=True)
            self.ensure_routines(user, now)
            return text

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
        turn = await self.llm.think(system, messages)
        self._apply(user, turn)
        text = turn.message.strip()
        if not text:
            return None
        self.storage.add_message(user.id, "bot", text, proactive=proactive)
        return text

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
