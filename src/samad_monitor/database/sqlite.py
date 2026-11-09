"""مدیریت اتصال و مهاجرت اسکیما SQLite با حالت WAL و حفاظت دسترسی فایل."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from samad_monitor.browser.session import restrict_path_permissions

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS menus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL UNIQUE,
    week_end TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'api'
);

CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    menu_id INTEGER NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    day_name TEXT NOT NULL,
    meal_type TEXT NOT NULL,
    meal_type_id INTEGER NOT NULL DEFAULT 0,
    restaurant TEXT NOT NULL,
    self_id INTEGER NOT NULL DEFAULT 0,
    is_free_restaurant INTEGER NOT NULL DEFAULT 0,
    food_name TEXT NOT NULL,
    availability TEXT NOT NULL DEFAULT 'OPEN',
    program_id INTEGER,
    food_type_id INTEGER,
    side_dishes TEXT NOT NULL DEFAULT '',
    price_rials INTEGER NOT NULL DEFAULT 0,
    quota_limit TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_meals_menu_date ON meals(menu_id, date);

CREATE TABLE IF NOT EXISTS menu_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,
    week_end TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    event_type TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    meals_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_week ON menu_snapshots(week_start, captured_at);

CREATE TABLE IF NOT EXISTS sent_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    week_start TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    channels_succeeded TEXT NOT NULL DEFAULT '[]',
    channels_failed TEXT NOT NULL DEFAULT '[]',
    fully_delivered INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS check_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    target_week_start TEXT NOT NULL,
    target_week_end TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'api',
    meals_count INTEGER NOT NULL DEFAULT 0,
    event_id TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_check_history_time ON check_history(checked_at DESC);

CREATE TABLE IF NOT EXISTS error_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    http_status INTEGER,
    samad_code INTEGER
);

CREATE INDEX IF NOT EXISTS idx_error_logs_time ON error_logs(logged_at DESC);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_subscribers (
    chat_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    chat_type TEXT NOT NULL DEFAULT 'private',
    role TEXT NOT NULL DEFAULT 'subscriber',
    enabled INTEGER NOT NULL DEFAULT 1,
    added_via TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_recent_contacts (
    chat_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    chat_type TEXT NOT NULL DEFAULT 'private',
    last_message TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL
);
"""


class DatabaseManager:
    """مدیریت ایمن اتصال به دیتابیس SQLite و ساخت جداول مورد نیاز."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def initialize(self) -> None:
        with self.transaction() as conn:
            conn.executescript(SCHEMA_SQL)
        restrict_path_permissions(self.db_path, is_dir=False)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
