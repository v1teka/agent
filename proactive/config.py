"""Settings loaded from environment variables (see .env.example)."""

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
    allowed_user_ids: set[int] = field(default_factory=set)
    model: str = "claude-opus-5-5"
    effort: str = "low"
    db_path: Path = Path("data/proactive.db")
    timezone: ZoneInfo = ZoneInfo("Europe/Moscow")
    role: str = "mentor"
    morning_at: time = time(9, 0)
    evening_at: time = time(21, 30)
    quiet_start: time = time(23, 0)
    quiet_end: time = time(8, 0)
    daily_message_budget: int = 4
    history_limit: int = 30
    tick_seconds: int = 60

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        _load_dotenv(Path(env_file))
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise SystemExit("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        env = os.environ.get
        return cls(
            telegram_token=token,
            allowed_user_ids=_parse_ids(env("ALLOWED_USER_IDS", "")),
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
        )
