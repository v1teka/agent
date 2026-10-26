"""agent settings loaded from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the bot runs without extra dependencies."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _parse_time(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _parse_ids(value: str) -> set[int]:
    return {int(v) for v in value.replace(" ", "").split(",") if v}


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    admin_user_ids: set[int] = field(default_factory=set)
    public: bool = True
    allowed_user_ids: set[int] = field(default_factory=set)  # extra users when the bot is private
    model: str = "claude-opus-5-5"
    effort: str = "low"
    db_path: Path = Path("data/agent.db")
    timezone: ZoneInfo = ZoneInfo("Europe/Moscow")
    role: str = "mentor"
    morning_at: time = time(9, 0)
    evening_at: time = time(21, 30)
    quiet_start: time = time(23, 0)
    quiet_end: time = time(8, 0)
    daily_message_budget: int = 4
    history_limit: int = 30
    tick_seconds: int = 60
    # Abuse protection. Admins are exempt from all limits.
    user_daily_budget_usd: float = 0.50
    global_daily_budget_usd: float = 3.00
    user_messages_per_minute: int = 5
    user_messages_per_day: int = 60
    max_message_chars: int = 2000
    # USD per million tokens, used to estimate spend (defaults: claude-opus-5-5).
    price_input: float = 4.00
    price_output: float = 20.00
    price_cache_read: float = 0.40
    price_cache_write: float = 5.00

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        _load_dotenv(Path(env_file))
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise SystemExit("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        env = os.environ.get
        allowed = _parse_ids(env("ALLOWED_USER_IDS", ""))
        # Older configs only had ALLOWED_USER_IDS, which held the owner's id.
        admins = _parse_ids(env("ADMIN_USER_IDS", "")) or allowed
        return cls(
            telegram_token=token,
            admin_user_ids=admins,
            public=env("ACCESS", "public").strip().lower() != "private",
            allowed_user_ids=allowed,
            model=env("CLAUDE_MODEL", cls.model),
            effort=env("CLAUDE_EFFORT", cls.effort),
            db_path=Path(env("DB_PATH", str(cls.db_path))),
            timezone=ZoneInfo(env("TIMEZONE", "Europe/Moscow")),
            role=env("ROLE", cls.role),
            morning_at=_parse_time(env("MORNING_AT", "09:00")),
            evening_at=_parse_time(env("EVENING_AT", "21:30")),
            quiet_start=_parse_time(env("QUIET_START", "23:00")),
            quiet_end=_parse_time(env("QUIET_END", "08:00")),
            daily_message_budget=int(env("DAILY_MESSAGE_BUDGET", "4")),
            history_limit=int(env("HISTORY_LIMIT", "30")),
            tick_seconds=int(env("TICK_SECONDS", "60")),
            user_daily_budget_usd=float(env("USER_DAILY_BUDGET_USD", "0.50")),
            global_daily_budget_usd=float(env("GLOBAL_DAILY_BUDGET_USD", "3.00")),
            user_messages_per_minute=int(env("USER_MESSAGES_PER_MINUTE", "5")),
            user_messages_per_day=int(env("USER_MESSAGES_PER_DAY", "60")),
            max_message_chars=int(env("MAX_MESSAGE_CHARS", "2000")),
            price_input=float(env("PRICE_INPUT_PER_MTOK", "4.00")),
            price_output=float(env("PRICE_OUTPUT_PER_MTOK", "20.00")),
            price_cache_read=float(env("PRICE_CACHE_READ_PER_MTOK", "0.40")),
            price_cache_write=float(env("PRICE_CACHE_WRITE_PER_MTOK", "5.00")),
        )

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids

    def may_use(self, user_id: int) -> bool:
        return self.public or self.is_admin(user_id) or user_id in self.allowed_user_ids
