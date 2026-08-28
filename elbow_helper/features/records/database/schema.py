"""SQLite schema for leadership records."""

from __future__ import annotations

from contextlib import closing
import sqlite3


class RecordSchema:
    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS leadership_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    member_id INTEGER NOT NULL,
                    member_display TEXT NOT NULL,
                    category_key TEXT NOT NULL,
                    incident_type_key TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    recorder_id INTEGER NOT NULL,
                    recorder_display TEXT NOT NULL,
                    edited_by_id INTEGER,
                    edited_by_display TEXT,
                    removed_ts INTEGER,
                    removed_by_id INTEGER,
                    removed_by_display TEXT
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_leadership_records_member_status "
                "ON leadership_records(member_id, status, created_ts DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_leadership_records_created "
                "ON leadership_records(created_ts DESC)"
            )
            conn.commit()
