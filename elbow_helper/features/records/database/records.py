"""Leadership record write operations."""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any


class RecordWriter:
    def _insert_record(
        self,
        *,
        created_ts: int,
        member_id: int,
        member_display: str,
        category_key: str,
        incident_type_key: str,
        note: str,
        recorder_id: int,
        recorder_display: str,
    ) -> dict[str, Any]:
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute(
                """
                INSERT INTO leadership_records (
                    created_ts, updated_ts, status, member_id, member_display,
                    category_key, incident_type_key, note, recorder_id,
                    recorder_display
                ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(created_ts), int(created_ts), int(member_id), member_display,
                    category_key, incident_type_key, note, int(recorder_id),
                    recorder_display,
                ),
            )
            record_id = int(cursor.lastrowid)
            conn.commit()
            row = cursor.execute(
                "SELECT * FROM leadership_records WHERE id = ?", (record_id,)
            ).fetchone()
        return dict(row)

    def _update_record(
        self,
        *,
        record_id: int,
        member_id: int,
        category_key: str,
        incident_type_key: str,
        note: str,
        updated_ts: int,
        edited_by_id: int,
        edited_by_display: str,
    ) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute(
                """
                UPDATE leadership_records
                SET category_key = ?, incident_type_key = ?, note = ?,
                    updated_ts = ?, edited_by_id = ?, edited_by_display = ?
                WHERE id = ? AND member_id = ? AND status = 'active'
                """,
                (
                    category_key, incident_type_key, note, int(updated_ts),
                    int(edited_by_id), edited_by_display, int(record_id),
                    int(member_id),
                ),
            )
            if cursor.rowcount <= 0:
                return None
            conn.commit()
            row = cursor.execute(
                "SELECT * FROM leadership_records WHERE id = ?", (int(record_id),)
            ).fetchone()
        return dict(row) if row else None

    def _remove_record(
        self,
        *,
        record_id: int,
        member_id: int,
        removed_ts: int,
        removed_by_id: int,
        removed_by_display: str,
    ) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute(
                """
                UPDATE leadership_records
                SET status = 'removed', updated_ts = ?, removed_ts = ?,
                    removed_by_id = ?, removed_by_display = ?
                WHERE id = ? AND member_id = ? AND status = 'active'
                """,
                (
                    int(removed_ts), int(removed_ts), int(removed_by_id),
                    removed_by_display, int(record_id), int(member_id),
                ),
            )
            if cursor.rowcount <= 0:
                return None
            conn.commit()
            row = cursor.execute(
                "SELECT * FROM leadership_records WHERE id = ?", (int(record_id),)
            ).fetchone()
        return dict(row) if row else None
