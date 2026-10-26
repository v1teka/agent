"""Telegram front-end: commands, incoming messages and the scheduler tick."""

from __future__ import annotations

import logging
from collections import defaultdict

from telegram import Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Settings
from datetime import timedelta

from .core import BLOCKED, Core
from .schedule import local_day_start
from .storage import utcnow

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000

HELP = (
    "Я пишу первым: утром даю задание на день, вечером спрашиваю, как прошло, "
    "а иногда сам напоминаю о чём-то между делом.\n\n"
    "Рассказывай о себе что угодно и когда угодно: каждая мелочь помогает подбирать задания точнее.\n\n"
    "/me — что я о тебе помню\n"
    "/forget N — забыть факт номер N\n"
    "/tasks — последние задания\n"
    "/pause — перестать писать первым\n"
    "/resume — снова писать первым\n"
    "/id — твой Telegram id"
)

ADMIN_HELP = (
    "\n\nДля админа:\n"
    "/stats — кто сколько потратил\n"
    "/block ID — заблокировать пользователя\n"
    "/unblock ID — разблокировать"
)

CATEGORY_RU = {
    "biography": "Биография", "education": "Образование", "work": "Работа", "income": "Доходы",
    "health": "Здоровье", "habits": "Привычки", "interests": "Интересы", "relationships": "Отношения",
    "goals": "Цели и планы", "character": "Характер", "preferences": "Предпочтения", "other": "Другое",
}
STATUS_RU = {"open": "в работе", "done": "сделано", "skipped": "пропущено"}


def _chunks(text: str) -> list[str]:
    return [text[i:i + TELEGRAM_LIMIT] for i in range(0, len(text), TELEGRAM_LIMIT)] or [""]


class TelegramBot:
    def __init__(self, settings: Settings, core: Core):
        self.settings = settings
        self.core = core
        self.storage = core.storage
        self.app = Application.builder().token(settings.telegram_token).build()
        self._register()

    def run(self) -> None:
        if not self.settings.admin_user_ids:
            log.warning("ADMIN_USER_IDS is empty: nobody can see spending or block users. Send /id to the bot.")
        self.app.job_queue.run_repeating(self._tick, interval=self.settings.tick_seconds, first=5)
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)

    # plumbing --------------------------------------------------------------

    def _register(self) -> None:
        private = filters.ChatType.PRIVATE
        add = self.app.add_handler
        add(CommandHandler("start", self._start, filters=private))
        add(CommandHandler("help", self._help, filters=private))
        add(CommandHandler("me", self._me, filters=private))
        add(CommandHandler("forget", self._forget, filters=private))
        add(CommandHandler("tasks", self._tasks, filters=private))
        add(CommandHandler("pause", self._pause, filters=private))
        add(CommandHandler("resume", self._resume, filters=private))
        add(CommandHandler("id", self._id, filters=private))
        add(CommandHandler("stats", self._stats, filters=private))
        add(CommandHandler("block", self._block, filters=private))
        add(CommandHandler("unblock", self._unblock, filters=private))
        add(MessageHandler(private & filters.TEXT & ~filters.COMMAND, self._text))
        self.app.add_error_handler(self._error)

    async def _allowed(self, update: Update) -> bool:
        chat = update.effective_chat
        if chat is None or chat.type != ChatType.PRIVATE:
            return False
        if not self.settings.may_use(chat.id):
            log.info("Rejected %s (%s): the bot is private", chat.id, chat.username)
            await update.effective_message.reply_text("Это закрытый бот.")
            return False
        user = self.storage.get_user(chat.id)
        if user and user.blocked:
            await update.effective_message.reply_text(BLOCKED)
            return False
        return True

    async def _admin(self, update: Update) -> bool:
        return update.effective_chat is not None and self.settings.is_admin(update.effective_chat.id)

    async def _flush_alerts(self) -> None:
        while self.core.alerts:
            alert = self.core.alerts.pop(0)
            for admin in self.settings.admin_user_ids:
                try:
                    await self.app.bot.send_message(admin, alert)
                except Exception:
                    log.exception("Could not alert admin %s", admin)

    async def _send(self, chat_id: int, text: str | None) -> None:
        if not text:
            return
        for part in _chunks(text):
            await self.app.bot.send_message(chat_id, part)

    @staticmethod
    def _name(update: Update) -> str | None:
        user = update.effective_user
        return user.first_name if user else None

    @staticmethod
    def _username(update: Update) -> str | None:
        user = update.effective_user
        return user.username if user else None

    # handlers --------------------------------------------------------------

    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        await self._send(chat_id, await self.core.start(chat_id, self._name(update), self._username(update)))
        await self._flush_alerts()

    async def _help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._allowed(update):
            await update.effective_message.reply_text(HELP + (ADMIN_HELP if await self._admin(update) else ""))

    async def _text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        reply = await self.core.on_message(
            chat_id, self._name(update), update.effective_message.text, self._username(update)
        )
        await self._send(chat_id, reply)
        await self._flush_alerts()

    async def _me(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        facts = self.storage.facts(update.effective_chat.id)
        if not facts:
            await update.effective_message.reply_text(
                "Пока я почти ничего о тебе не знаю. Расскажи что угодно: чем занимаешься, "
                "что нравится, чего хочешь. Чем больше знаю, тем точнее задания."
            )
            return
        groups: dict[str, list[str]] = defaultdict(list)
        for f in facts:
            groups[f.category].append(f"{f.id}. {f.text}")
        lines = []
        for category, items in groups.items():
            lines.append(CATEGORY_RU.get(category, category))
            lines.extend(items)
            lines.append("")
        empty = [CATEGORY_RU[c] for c in CATEGORY_RU if c not in groups and c != "other"]
        if empty:
            lines.append("Пока пусто: " + ", ".join(empty).lower() + ". Можешь рассказать в любой момент.")
        lines.append("Забыть факт: /forget N")
        await self._send(update.effective_chat.id, "\n".join(lines))

    async def _forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        if not context.args or not context.args[0].isdigit():
            await update.effective_message.reply_text("Напиши номер факта из /me, например: /forget 3")
            return
        ok = self.storage.delete_fact(update.effective_chat.id, int(context.args[0]))
        await update.effective_message.reply_text("Забыл." if ok else "Не нашёл такой факт.")

    async def _tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        tasks = self.storage.recent_tasks(update.effective_chat.id, 10)
        if not tasks:
            await update.effective_message.reply_text("Заданий пока не было.")
            return
        lines = [
            f"#{t.id} {t.title} — {STATUS_RU.get(t.status, t.status)}" + (f" ({t.note})" if t.note else "")
            for t in tasks
        ]
        await self._send(update.effective_chat.id, "\n".join(lines))

    async def _pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._allowed(update):
            self.storage.set_paused(update.effective_chat.id, True)
            await update.effective_message.reply_text("Хорошо, не пишу первым. Вернуть: /resume")

    async def _resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._allowed(update):
            chat_id = update.effective_chat.id
            self.storage.set_paused(chat_id, False)
            user = self.storage.get_user(chat_id)
            if user:
                self.core.ensure_routines(user)
            await update.effective_message.reply_text("Снова на связи, буду писать первым.")

    async def _id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(f"Твой Telegram id: {update.effective_chat.id}")

    async def _stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._admin(update):
            return
        now = utcnow()
        today = local_day_start(now, self.settings.timezone)
        day = {r.user_id: r for r in self.storage.usage_report(today)}
        week = {r.user_id: r for r in self.storage.usage_report(today - timedelta(days=6))}
        total = self.storage.usage_report()
        spent = self.storage.spend_since(today)
        lines = [
            f"Сегодня потрачено ${spent:.2f} из общего лимита ${self.settings.global_daily_budget_usd:.2f}.",
            f"Лимит на человека в день: ${self.settings.user_daily_budget_usd:.2f}.",
            "",
            "Сегодня · за 7 дней · всего:",
        ]
        for r in total[:30]:
            who = " ".join(p for p in (r.name, f"@{r.username}" if r.username else None) if p) or "без имени"
            mark = " [заблокирован]" if r.blocked else ""
            mark += " [админ]" if self.settings.is_admin(r.user_id) else ""
            lines.append(
                f"{who}, id {r.user_id}{mark}: ${day[r.user_id].cost_usd:.2f} ({day[r.user_id].turns} отв.)"
                f" · ${week[r.user_id].cost_usd:.2f} · ${r.cost_usd:.2f}"
            )
        if len(total) > 30:
            lines.append(f"…и ещё {len(total) - 30}")
        lines.append("")
        lines.append("Заблокировать: /block ID")
        await self._send(update.effective_chat.id, "\n".join(lines))

    async def _set_blocked(self, update: Update, context: ContextTypes.DEFAULT_TYPE, blocked: bool) -> None:
        if not await self._admin(update):
            return
        command = "/block" if blocked else "/unblock"
        if not context.args or not context.args[0].lstrip("-").isdigit():
            await update.effective_message.reply_text(f"Напиши id из /stats, например: {command} 123456")
            return
        target = int(context.args[0])
        if blocked and self.settings.is_admin(target):
            await update.effective_message.reply_text("Админа заблокировать нельзя.")
            return
        if not self.storage.set_blocked(target, blocked):
            await update.effective_message.reply_text("Такого пользователя нет.")
            return
        if not blocked:
            user = self.storage.get_user(target)
            self.core.ensure_routines(user)
        await update.effective_message.reply_text("Заблокирован." if blocked else "Разблокирован.")

    async def _block(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_blocked(update, context, True)

    async def _unblock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set_blocked(update, context, False)

    async def _tick(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        for checkin in self.storage.due_checkins(utcnow()):
            try:
                text = await self.core.fire(checkin)
                await self._send(checkin.user_id, text)
            except Exception:
                log.exception("Check-in %s failed", checkin.id)
        await self._flush_alerts()

    async def _error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("Handler error", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Что-то пошло не так, попробуй ещё раз чуть позже.")
