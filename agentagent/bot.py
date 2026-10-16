"""Telegram front-end: commands, incoming messages and the scheduler tick."""

from __future__ import annotations

import logging
from collections import defaultdict

from telegram import Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Settings
from .core import Core
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
    "/resume — снова писать первым"
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
        if not self.settings.allowed_user_ids:
            log.warning("ALLOWED_USER_IDS is empty: anyone who finds the bot can use it (and your API key).")
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
        add(MessageHandler(private & filters.TEXT & ~filters.COMMAND, self._text))
        self.app.add_error_handler(self._error)

    async def _allowed(self, update: Update) -> bool:
        chat = update.effective_chat
        if chat is None or chat.type != ChatType.PRIVATE:
            return False
        allowed = self.settings.allowed_user_ids
        if allowed and chat.id not in allowed:
            await update.effective_message.reply_text(
                f"Это личный бот. Твой Telegram id: {chat.id}. "
                "Если это твой бот, добавь id в ALLOWED_USER_IDS в файле .env и перезапусти его."
            )
            return False
        return True

    async def _send(self, chat_id: int, text: str | None) -> None:
        if not text:
            return
        for part in _chunks(text):
            await self.app.bot.send_message(chat_id, part)

    @staticmethod
    def _name(update: Update) -> str | None:
        user = update.effective_user
        return user.first_name if user else None

    # handlers --------------------------------------------------------------

    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        await self._send(chat_id, await self.core.start(chat_id, self._name(update)))

    async def _help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._allowed(update):
            await update.effective_message.reply_text(HELP)

    async def _text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._allowed(update):
            return
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        reply = await self.core.on_message(chat_id, self._name(update), update.effective_message.text)
        await self._send(chat_id, reply)

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

    async def _tick(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        for checkin in self.storage.due_checkins(utcnow()):
            try:
                text = await self.core.fire(checkin)
                await self._send(checkin.user_id, text)
            except Exception:
                log.exception("Check-in %s failed", checkin.id)

    async def _error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("Handler error", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Что-то пошло не так, попробуй ещё раз чуть позже.")
