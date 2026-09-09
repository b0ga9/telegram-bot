from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from threading import RLock
from typing import Optional


class SQLiteStorage:
    """Persistent SQLite state for TRD PULSE deduplication, cooldowns and stats."""

    def __init__(self, db_path: str = "trd_pulse.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS published_events (
                    event_type TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    published_at REAL NOT NULL,
                    PRIMARY KEY (event_type, event_key)
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS cooldowns (
                    channel TEXT PRIMARY KEY,
                    last_published_at REAL NOT NULL
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS stat_times (
                    name TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL
                )
            """)

    def is_duplicate(self, event_type: str, event_key: str) -> bool:
        if not event_key:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM published_events WHERE event_type = ? AND event_key = ? LIMIT 1",
                (event_type, event_key),
            ).fetchone()
        return row is not None

    def cooldown_active(self, channel: str, cooldown_seconds: float, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT last_published_at FROM cooldowns WHERE channel = ?", (channel,)
            ).fetchone()
        if row is None:
            return False
        return (now - float(row[0])) < float(cooldown_seconds)

    def last_published_at(self, channel: str) -> Optional[float]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_published_at FROM cooldowns WHERE channel = ?", (channel,)
            ).fetchone()
        return float(row[0]) if row else None

    def mark_published(self, event_type: str, event_key: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO published_events(event_type, event_key, published_at) VALUES (?, ?, ?)",
                (event_type, event_key, now),
            )
            self._conn.execute(
                """
                INSERT INTO cooldowns(channel, last_published_at) VALUES (?, ?)
                ON CONFLICT(channel) DO UPDATE SET last_published_at = excluded.last_published_at
                """,
                (event_type, now),
            )

    def increment_stat(self, name: str, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO stats(name, value) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET value = value + 1",
                (name,),
            )
            self._conn.execute(
                "INSERT INTO stat_times(name, timestamp) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET timestamp = excluded.timestamp",
                (name, now),
            )
            row = self._conn.execute("SELECT value FROM stats WHERE name = ?", (name,)).fetchone()
        return int(row[0])

    def get_stat(self, name: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT value FROM stats WHERE name = ?", (name,)).fetchone()
        return int(row[0]) if row else 0

    def get_stat_time(self, name: str) -> Optional[float]:
        with self._lock:
            row = self._conn.execute("SELECT timestamp FROM stat_times WHERE name = ?", (name,)).fetchone()
        return float(row[0]) if row else None

    def get_stats(self) -> dict[str, int | float | None]:
        names = (
            "market_checks", "news_checks", "market_published",
            "pulse_published", "news_published",
        )
        return {
            **{name: self.get_stat(name) for name in names},
            "last_market_check_at": self.get_stat_time("market_checks"),
            "last_news_check_at": self.get_stat_time("news_checks"),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
