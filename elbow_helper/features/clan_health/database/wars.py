"""Bounded historical regular-war reads from Clan Health storage."""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any


class ClanHealthWarReads:
    def regular_war_history(
        self, *, clan_code: str, history_limit: int,
        before_war_id: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        limit = max(1, min(int(history_limit), 21))
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN")
            cursor = None
            if before_war_id is not None:
                cursor = connection.execute(
                    """
                    SELECT end_ts, war_id FROM wars
                    WHERE war_id = ? AND clan_code = ?
                      AND war_type = 'REG' AND state = 'warEnded'
                    """,
                    (before_war_id, clan_code),
                ).fetchone()
                if cursor is None:
                    raise ValueError("Invalid regular-war history cursor")
            wars = [dict(row) for row in connection.execute(
                """
                SELECT war_id, war_type, clan_code, clan_tag,
                       opponent_tag, opponent_name, team_size,
                       attacks_per_member, state, preparation_start_ts,
                       start_ts, end_ts, last_seen_ts, source
                FROM wars
                WHERE clan_code = ? AND war_type = 'REG' AND state = 'warEnded'
                  AND (
                    ? IS NULL OR end_ts < ?
                    OR (end_ts = ? AND war_id < ?)
                  )
                ORDER BY end_ts DESC, war_id DESC
                LIMIT ?
                """,
                (
                    clan_code, before_war_id,
                    cursor["end_ts"] if cursor else None,
                    cursor["end_ts"] if cursor else None,
                    cursor["war_id"] if cursor else None,
                    limit,
                ),
            ).fetchall()]
            if not wars:
                return {"wars": [], "roster": [], "attacks": []}
            war_ids = [str(row["war_id"]) for row in wars]
            placeholders = ",".join("?" for _ in war_ids)
            roster = [dict(row) for row in connection.execute(
                f"""
                SELECT war_id, clan_code, player_tag, player_name,
                       townhall, map_position, attacks_expected,
                       attacks_used, roster_state, captured_ts, source
                FROM war_roster_members
                WHERE clan_code = ? AND war_id IN ({placeholders})
                ORDER BY war_id, map_position, player_name, player_tag
                """,
                [clan_code, *war_ids],
            ).fetchall()]
            attacks = [dict(row) for row in connection.execute(
                f"""
                SELECT war_id, clan_code, player_tag, player_name,
                       attack_order, defender_tag, defender_name,
                       defender_map_position, defender_townhall,
                       stars, destruction, fresh_attack, duration,
                       end_ts, war_state, source
                FROM war_attacks
                WHERE clan_code = ? AND war_type = 'REG'
                  AND war_id IN ({placeholders})
                ORDER BY war_id, player_tag, attack_order, defender_tag
                """,
                [clan_code, *war_ids],
            ).fetchall()]
        return {"wars": wars, "roster": roster, "attacks": attacks}
