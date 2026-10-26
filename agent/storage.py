"""SQLite memory: people, conversation, facts about the person, tasks and planned check-ins.

All timestamps are stored as UTC ISO strings.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,            -- Telegram chat id
    name TEXT,
    role TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_user_message_at TEXT,
    unanswered_proactive INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    sender TEXT NOT NULL CHECK (sender IN ('user', 'bot')),
    text TEXT NOT NULL,
    proactive INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',  -- open | done | skipped
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    due_at TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- routine name, or 'followup'
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | skipped
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_time ON usage (created_at, user_id);
CREATE INDEX IF NOT EXISTS idx_checkins_due ON checkins (status, due_at);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages (user_id, id);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# Columns added after the first release; created on start if missing.
MIGRATIONS = {
    "users": {
        "username": "TEXT",
        "blocked": "INTEGER NOT NULL DEFAULT 0",
    },
}


@dataclass
class User:
    id: int
    name: str | None
    role: str
    paused: bool
    created_at: datetime
    last_user_message_at: datetime | None
    unanswered_proactive: int
    username: str | None = None
    blocked: bool = False


@dataclass
class UsageRow:
    user_id: int
    name: str | None
    username: str | None
    blocked: bool
    turns: int
    tokens: int
    cost_usd: float


@dataclass
class Message:
    sender: str
    text: str
    proactive: bool
    created_at: datetime


@dataclass
class Fact:
    id: int
    category: str
    text: str


@dataclass
class Task:
    id: int
    title: str
    details: str
    status: str
    note: str
    created_at: datetime


@dataclass
class Checkin:
    id: int
    user_id: int
    due_at: datetime
    kind: str
    reason: str


class Storage:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for column, decl in columns.items():
                if column not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # users -----------------------------------------------------------------

    def get_user(self, user_id: int) -> User | None:
        row = self.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        return User(
            id=row["id"],
            name=row["name"],
            role=row["role"],
            paused=bool(row["paused"]),
            created_at=_dt(row["created_at"]),
            last_user_message_at=_dt(row["last_user_message_at"]),
            unanswered_proactive=row["unanswered_proactive"],
            username=row["username"],
            blocked=bool(row["blocked"]),
        )

    def ensure_user(
        self, user_id: int, name: str | None, role: str, username: str | None = None
    ) -> tuple[User, bool]:
        """Return the user, creating them if needed. The flag is True for a new user."""
        existing = self.get_user(user_id)
        if existing:
            if (name, username) != (existing.name, existing.username) and (name or username):
                self.db.execute(
                    "UPDATE users SET name = COALESCE(?, name), username = COALESCE(?, username) WHERE id = ?",
                    (name, username, user_id),
                )
                self.db.commit()
                return self.get_user(user_id), False
            return existing, False
        self.db.execute(
            "INSERT INTO users (id, name, username, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, username, role, _iso(utcnow())),
        )
        self.db.commit()
        return self.get_user(user_id), True

    def list_users(self) -> list[User]:
        ids = [r["id"] for r in self.db.execute("SELECT id FROM users")]
        return [self.get_user(i) for i in ids]

    def set_paused(self, user_id: int, paused: bool) -> None:
        self.db.execute("UPDATE users SET paused = ? WHERE id = ?", (int(paused), user_id))
        self.db.commit()

    def set_blocked(self, user_id: int, blocked: bool) -> bool:
        cur = self.db.execute("UPDATE users SET blocked = ? WHERE id = ?", (int(blocked), user_id))
        if blocked:
            self.db.execute(
                "UPDATE checkins SET status = 'skipped' WHERE user_id = ? AND status = 'pending'", (user_id,)
            )
        self.db.commit()
        return cur.rowcount > 0

    # messages --------------------------------------------------------------

    def add_message(self, user_id: int, sender: str, text: str, proactive: bool = False) -> None:
        now = _iso(utcnow())
        self.db.execute(
            "INSERT INTO messages (user_id, sender, text, proactive, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, sender, text, int(proactive), now),
        )
        if sender == "user":
            self.db.execute(
                "UPDATE users SET last_user_message_at = ?, unanswered_proactive = 0 WHERE id = ?",
                (now, user_id),
            )
        elif proactive:
            self.db.execute(
                "UPDATE users SET unanswered_proactive = unanswered_proactive + 1 WHERE id = ?",
                (user_id,),
            )
        self.db.commit()

    def recent_messages(self, user_id: int, limit: int) -> list[Message]:
        rows = self.db.execute(
            "SELECT * FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        return [
            Message(r["sender"], r["text"], bool(r["proactive"]), _dt(r["created_at"]))
            for r in reversed(rows)
        ]

    def user_messages_since(self, user_id: int, since: datetime) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE user_id = ? AND sender = 'user' AND created_at >= ?",
            (user_id, _iso(since)),
        ).fetchone()
        return row[0]

    def proactive_count_since(self, user_id: int, since: datetime) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE user_id = ? AND proactive = 1 AND created_at >= ?",
            (user_id, _iso(since)),
        ).fetchone()
        return row[0]

    # facts -----------------------------------------------------------------

    def add_fact(self, user_id: int, category: str, text: str) -> None:
        duplicate = self.db.execute(
            "SELECT 1 FROM facts WHERE user_id = ? AND lower(text) = lower(?)", (user_id, text)
        ).fetchone()
        if duplicate:
            return
        self.db.execute(
            "INSERT INTO facts (user_id, category, text, created_at) VALUES (?, ?, ?, ?)",
            (user_id, category, text, _iso(utcnow())),
        )
        self.db.commit()

    def facts(self, user_id: int) -> list[Fact]:
        rows = self.db.execute(
            "SELECT id, category, text FROM facts WHERE user_id = ? ORDER BY category, id", (user_id,)
        ).fetchall()
        return [Fact(r["id"], r["category"], r["text"]) for r in rows]

    def delete_fact(self, user_id: int, fact_id: int) -> bool:
        cur = self.db.execute("DELETE FROM facts WHERE user_id = ? AND id = ?", (user_id, fact_id))
        self.db.commit()
        return cur.rowcount > 0

    # tasks -----------------------------------------------------------------

    def add_task(self, user_id: int, title: str, details: str) -> int:
        now = _iso(utcnow())
        cur = self.db.execute(
            "INSERT INTO tasks (user_id, title, details, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, title, details, now, now),
        )
        self.db.commit()
        return cur.lastrowid

    def update_task(self, user_id: int, task_id: int, status: str, note: str) -> bool:
        cur = self.db.execute(
            "UPDATE tasks SET status = ?, note = ?, updated_at = ? WHERE user_id = ? AND id = ?",
            (status, note, _iso(utcnow()), user_id, task_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    def recent_tasks(self, user_id: int, limit: int = 10) -> list[Task]:
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        return [
            Task(r["id"], r["title"], r["details"], r["status"], r["note"], _dt(r["created_at"]))
            for r in rows
        ]

    # usage -----------------------------------------------------------------

    def add_usage(
        self, user_id: int, input_tokens: int, output_tokens: int,
        cache_read: int, cache_write: int, cost_usd: float,
    ) -> None:
        self.db.execute(
            "INSERT INTO usage (user_id, created_at, input_tokens, output_tokens, cache_read_tokens,"
            " cache_write_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, _iso(utcnow()), input_tokens, output_tokens, cache_read, cache_write, cost_usd),
        )
        self.db.commit()

    def spend_since(self, since: datetime, user_id: int | None = None) -> float:
        query = "SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE created_at >= ?"
        args: tuple = (_iso(since),)
        if user_id is not None:
            query += " AND user_id = ?"
            args += (user_id,)
        return self.db.execute(query, args).fetchone()[0]

    def usage_report(self, since: datetime | None = None) -> list[UsageRow]:
        """Spend per user, most expensive first. Users without spend are included."""
        rows = self.db.execute(
            """
            SELECT u.id, u.name, u.username, u.blocked,
                   COUNT(g.id) AS turns,
                   COALESCE(SUM(g.input_tokens + g.output_tokens + g.cache_read_tokens + g.cache_write_tokens), 0) AS tokens,
                   COALESCE(SUM(g.cost_usd), 0) AS cost
            FROM users u
            LEFT JOIN usage g ON g.user_id = u.id AND g.created_at >= ?
            GROUP BY u.id ORDER BY cost DESC, u.created_at DESC
            """,
            (_iso(since) if since else "",),
        ).fetchall()
        return [
            UsageRow(r["id"], r["name"], r["username"], bool(r["blocked"]), r["turns"], r["tokens"], r["cost"])
            for r in rows
        ]

    # check-ins -------------------------------------------------------------

    def add_checkin(self, user_id: int, due_at: datetime, kind: str, reason: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO checkins (user_id, due_at, kind, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, _iso(due_at), kind, reason, _iso(utcnow())),
        )
        self.db.commit()
        return cur.lastrowid

    def has_pending(self, user_id: int, kind: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM checkins WHERE user_id = ? AND kind = ? AND status = 'pending'", (user_id, kind)
        ).fetchone()
        return row is not None

    def pending_checkins(self, user_id: int) -> list[Checkin]:
        rows = self.db.execute(
            "SELECT * FROM checkins WHERE user_id = ? AND status = 'pending' ORDER BY due_at", (user_id,)
        ).fetchall()
        return [self._checkin(r) for r in rows]

    def due_checkins(self, now: datetime) -> list[Checkin]:
        rows = self.db.execute(
            "SELECT * FROM checkins WHERE status = 'pending' AND due_at <= ? ORDER BY due_at", (_iso(now),)
        ).fetchall()
        return [self._checkin(r) for r in rows]

    def finish_checkin(self, checkin_id: int, status: str) -> None:
        self.db.execute("UPDATE checkins SET status = ? WHERE id = ?", (status, checkin_id))
        self.db.commit()

    def cancel_followups(self, user_id: int) -> None:
        self.db.execute(
            "UPDATE checkins SET status = 'skipped' WHERE user_id = ? AND kind = 'followup' AND status = 'pending'",
            (user_id,),
        )
        self.db.commit()

    @staticmethod
    def _checkin(r: sqlite3.Row) -> Checkin:
        return Checkin(r["id"], r["user_id"], _dt(r["due_at"]), r["kind"], r["reason"])
