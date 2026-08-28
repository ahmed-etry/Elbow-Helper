"""Leadership record read operations."""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any
from typing import Iterable


class RecordQueries:
    def _load_records(
        self,
        *,
        member_id: int | None = None,
        include_removed: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if member_id is not None:
            filters.append("member_id = ?")
            params.append(int(member_id))
        if not include_removed:
            filters.append("status = 'active'")
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(max(1, int(limit)))
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT * FROM leadership_records
                {where}
                ORDER BY created_ts DESC, id DESC
                {limit_sql}
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def active_for_members(
        self,
        member_ids: Iterable[int],
    ) -> list[dict[str, Any]]:
        ids = sorted(
            {
                int(member_id)
                for member_id in member_ids
                if int(member_id) > 0
            }
        )
        if not ids or not self.path.exists():
            return []
        placeholders = ",".join("?" for _ in ids)
        try:
            with closing(
                sqlite3.connect(self.path, timeout=10)
            ) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    f"""
                    SELECT *
                    FROM leadership_records
                    WHERE status = 'active'
                      AND member_id IN ({placeholders})
                    ORDER BY created_ts DESC, id DESC
                    """,
                    ids,
                ).fetchall()
        except sqlite3.Error:
            return []
        return [dict(row) for row in rows]
