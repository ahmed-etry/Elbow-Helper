"""Consistent reads of complete family-roster observations."""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any


class ClanHealthMovementReads:
    def complete_family_snapshots(
        self, *, run_limit: int, before_run_id: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Read consecutive complete family runs and all their roster rows."""
        limit = max(1, min(int(run_limit), 22))
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN")
            cursor = None
            if before_run_id is not None:
                cursor = connection.execute(
                    """
                    SELECT run_id, created_ts
                    FROM report_runs
                    WHERE run_id = ? AND scope = 'BACKGROUND_ALL' AND partial = 0
                    """,
                    (before_run_id,),
                ).fetchone()
                if cursor is None:
                    raise ValueError("Invalid family-snapshot cursor")
            runs = [dict(row) for row in connection.execute(
                """
                SELECT rr.run_id, rr.created_ts, COUNT(rp.player_tag) AS row_count
                FROM report_runs rr
                LEFT JOIN report_players rp ON rp.run_id = rr.run_id
                WHERE rr.scope = 'BACKGROUND_ALL' AND rr.partial = 0
                  AND (
                    ? IS NULL OR rr.created_ts < ?
                    OR (rr.created_ts = ? AND rr.run_id <= ?)
                  )
                GROUP BY rr.run_id
                ORDER BY rr.created_ts DESC, rr.run_id DESC
                LIMIT ?
                """,
                (
                    before_run_id,
                    cursor["created_ts"] if cursor else None,
                    cursor["created_ts"] if cursor else None,
                    cursor["run_id"] if cursor else None,
                    limit,
                ),
            ).fetchall()]
            if not runs:
                return {"runs": [], "rows": []}
            run_ids = [str(row["run_id"]) for row in runs]
            placeholders = ",".join("?" for _ in run_ids)
            rows = [dict(row) for row in connection.execute(
                f"""
                SELECT run_id, clan_code, player_tag, player_name
                FROM report_players
                WHERE run_id IN ({placeholders})
                ORDER BY run_id, clan_code, player_name COLLATE NOCASE, player_tag
                """,
                run_ids,
            ).fetchall()]
        return {"runs": runs, "rows": rows}


__all__ = ["ClanHealthMovementReads"]
