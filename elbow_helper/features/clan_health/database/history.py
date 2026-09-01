
"""Player history and trend reads for clan-health storage."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import sqlite3
from typing import Any, Dict, List, Optional

from ..config import UTC
from ..seasons import _season_key_for_datetime
class ClanHealthHistory:
    def _search_health_players(self, current: str, limit: int = 25) -> List[Dict[str, Any]]:
        needle = str(current or "").strip().lower()
        bounded_limit = max(1, min(25, int(limit)))
        conn = sqlite3.connect(self.path, timeout=0.25)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=250")
            cursor = conn.cursor()
            if not needle:
                cursor.execute(
                    """
                    SELECT player_tag, player_name, clan_code, townhall
                    FROM player_directory
                    ORDER BY last_seen_ts DESC, player_name_search, player_tag
                    LIMIT ?
                    """,
                    (bounded_limit,),
                )
                return [dict(row) for row in cursor.fetchall()]

            upper_bound = needle + chr(0x10FFFF)
            tag_needle = needle.removeprefix("#")
            tag_upper_bound = tag_needle + chr(0x10FFFF)
            cursor.execute(
                """
                SELECT
                    player_tag,
                    player_name,
                    clan_code,
                    townhall
                FROM player_directory
                WHERE
                    (player_name_search >= ? AND player_name_search < ?)
                    OR (player_tag_search >= ? AND player_tag_search < ?)
                    OR (clan_code_search >= ? AND clan_code_search < ?)
                ORDER BY
                    CASE
                        WHEN player_tag_search = ? THEN 0
                        WHEN player_name_search = ? THEN 1
                        WHEN player_tag_search >= ? AND player_tag_search < ? THEN 2
                        WHEN player_name_search >= ? AND player_name_search < ? THEN 3
                        ELSE 4
                    END,
                    last_seen_ts DESC,
                    player_name_search,
                    player_tag
                LIMIT ?
                """,
                (
                    needle,
                    upper_bound,
                    tag_needle,
                    tag_upper_bound,
                    needle,
                    upper_bound,
                    tag_needle,
                    needle,
                    tag_needle,
                    tag_upper_bound,
                    needle,
                    upper_bound,
                    bounded_limit,
                )
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def _latest_activity_season_for_player(self, player_tag: str) -> Optional[str]:
        """Find newest season using any stored activity, not only report rows."""
        with closing(sqlite3.connect(self.path)) as conn, conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT MAX(ts) AS latest_ts
                FROM (
                    SELECT MAX(captured_ts) AS ts FROM player_snapshots WHERE player_tag = ?
                    UNION ALL
                    SELECT MAX(end_ts) AS ts FROM war_attacks WHERE player_tag = ?
                    UNION ALL
                    SELECT MAX(end_ts) AS ts FROM raid_member_activity WHERE player_tag = ?
                )
                """,
                (player_tag, player_tag, player_tag),
            )
            row = cursor.fetchone()
            latest_ts = int(row[0]) if row and row[0] is not None else 0
        if latest_ts <= 0:
            return None
        season_key = _season_key_for_datetime(datetime.fromtimestamp(latest_ts, tz=UTC))
        return season_key

    def _load_snapshot_history(self, player_tag: str, limit: int = 60) -> List[Dict[str, Any]]:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT captured_ts, clan_code, player_name, townhall, hero_sum, games_total,
                       donations, donations_received, trophies
                FROM player_snapshots
                WHERE player_tag = ?
                ORDER BY captured_ts DESC
                LIMIT ?
                """,
                (player_tag, max(1, limit)),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            return rows

    def _load_player_war_attacks(
        self,
        *,
        player_tag: str,
        cycle_start: datetime,
        cycle_end: datetime,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if limit and int(limit) > 0:
                cursor.execute(
                    """
                    SELECT
                        war_id, war_type, clan_code, clan_tag, end_ts, war_state,
                        player_tag, player_name, attack_order,
                        defender_tag, defender_name, defender_map_position, defender_townhall,
                        stars, destruction, fresh_attack, duration, source
                    FROM war_attacks
                    WHERE player_tag = ? AND end_ts >= ? AND end_ts <= ?
                    ORDER BY end_ts DESC, war_id DESC, attack_order ASC
                    LIMIT ?
                    """,
                    (
                        player_tag,
                        int(cycle_start.timestamp()),
                        int(cycle_end.timestamp()),
                        max(1, limit),
                    ),
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        war_id, war_type, clan_code, clan_tag, end_ts, war_state,
                        player_tag, player_name, attack_order,
                        defender_tag, defender_name, defender_map_position, defender_townhall,
                        stars, destruction, fresh_attack, duration, source
                    FROM war_attacks
                    WHERE player_tag = ? AND end_ts >= ? AND end_ts <= ?
                    ORDER BY end_ts DESC, war_id DESC, attack_order ASC
                    """,
                    (
                        player_tag,
                        int(cycle_start.timestamp()),
                        int(cycle_end.timestamp()),
                    ),
                )
            rows = [dict(row) for row in cursor.fetchall()]
            return rows

    def _load_player_raid_member_activity(
        self,
        *,
        player_tag: str,
        cycle_start: datetime,
        cycle_end: datetime,
        limit: int = 120,
    ) -> List[Dict[str, Any]]:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if limit and int(limit) > 0:
                cursor.execute(
                    """
                    SELECT
                        weekend_id, clan_code, clan_tag, end_ts, player_tag, player_name,
                        attacks, attack_limit, bonus_attack_limit, attacks_expected, loot, source
                    FROM raid_member_activity AS rma
                    WHERE player_tag = ? AND end_ts >= ? AND end_ts <= ?
                    ORDER BY end_ts DESC, weekend_id DESC
                    LIMIT ?
                    """,
                    (
                        player_tag,
                        int(cycle_start.timestamp()),
                        int(cycle_end.timestamp()),
                        max(1, limit),
                    ),
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        weekend_id, clan_code, clan_tag, end_ts, player_tag, player_name,
                        attacks, attack_limit, bonus_attack_limit, attacks_expected, loot, source
                    FROM raid_member_activity AS rma
                    WHERE player_tag = ? AND end_ts >= ? AND end_ts <= ?
                    ORDER BY end_ts DESC, weekend_id DESC
                    """,
                    (
                        player_tag,
                        int(cycle_start.timestamp()),
                        int(cycle_end.timestamp()),
                    ),
                )
            rows = [dict(row) for row in cursor.fetchall()]
            return rows

    def _load_player_trend_history(
        self,
        *,
        player_tag: str,
        up_to_season_key: str,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    rp.*,
                    rr.created_ts
                FROM report_players rp
                JOIN report_runs rr ON rr.run_id = rp.run_id
                WHERE rp.player_tag = ?
                  AND rp.season_key <= ?
                  AND rr.partial = 0
                ORDER BY rp.season_key DESC, rr.created_ts DESC
                """,
                (player_tag, up_to_season_key),
            )
            raw = [dict(row) for row in cursor.fetchall()]

        # Keep one row per season (latest run).
        by_season: Dict[str, Dict[str, Any]] = {}
        for row in raw:
            key = str(row.get("season_key") or "")
            if key and key not in by_season:
                by_season[key] = row

        ordered = sorted(
            by_season.values(),
            key=lambda row: str(row.get("season_key") or ""),
            reverse=True,
        )[: max(1, limit)]
        return ordered

    def _load_player_movement_segments(
        self,
        *,
        player_tag: str,
        cycle_start: datetime,
        cycle_end: datetime,
        lookback_days: Optional[int] = 180,
    ) -> List[Dict[str, Any]]:
        window_end_ts = int(cycle_end.timestamp())
        conn = sqlite3.connect(self.path)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if lookback_days is None:
                cursor.execute(
                    """
                    SELECT captured_ts, clan_code, player_name
                    FROM player_snapshots
                    WHERE player_tag = ? AND captured_ts <= ?
                    ORDER BY captured_ts ASC
                    """,
                    (player_tag, window_end_ts),
                )
            else:
                window_start_ts = int((cycle_start - timedelta(days=max(0, lookback_days))).timestamp())
                cursor.execute(
                    """
                    SELECT captured_ts, clan_code, player_name
                    FROM player_snapshots
                    WHERE player_tag = ? AND captured_ts >= ? AND captured_ts <= ?
                    ORDER BY captured_ts ASC
                    """,
                    (player_tag, window_start_ts, window_end_ts),
                )
            points = [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

        if not points:
            return []

        segments: List[Dict[str, Any]] = []
        current = {
            "clan_code": str(points[0].get("clan_code") or "-"),
            "start_ts": int(points[0].get("captured_ts") or 0),
            "end_ts": int(points[0].get("captured_ts") or 0),
            "samples": 1,
        }
        for point in points[1:]:
            clan_code = str(point.get("clan_code") or "-")
            ts = int(point.get("captured_ts") or 0)
            if clan_code == current["clan_code"]:
                current["end_ts"] = ts
                current["samples"] += 1
                continue
            segments.append(current)
            current = {
                "clan_code": clan_code,
                "start_ts": ts,
                "end_ts": ts,
                "samples": 1,
            }
        segments.append(current)

        for seg in segments:
            duration_seconds = max(0, int(seg["end_ts"]) - int(seg["start_ts"]))
            seg["duration_hours"] = round(duration_seconds / 3600.0, 2)
            seg["duration_days"] = round(duration_seconds / 86400.0, 2)
        # Most recent first for reporting.
        segments.sort(key=lambda seg: int(seg["end_ts"]), reverse=True)
        return segments
