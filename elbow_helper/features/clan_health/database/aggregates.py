
"""Aggregate and baseline reads for clan-health storage."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional, Set

class ClanHealthAggregates:
    def _get_baseline_snapshot(self, player_tag: str, cutoff_ts: int) -> Optional[sqlite3.Row]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT townhall, hero_sum, pet_sum, equipment_sum, troop_sum, spell_sum, games_total, capital_contrib
                FROM player_snapshots
                WHERE player_tag = ? AND captured_ts <= ?
                ORDER BY captured_ts DESC
                LIMIT 1
                """,
                (player_tag, cutoff_ts),
            )
            # Baseline is the latest snapshot at or before cycle start.
            return cursor.fetchone()

    def _load_war_activity_breakdown(
        self,
        *,
        cycle_start_ts: int,
        cycle_end_ts: int,
        player_tags: Optional[Set[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            params: list[Any] = [cycle_start_ts, cycle_end_ts]
            where_player = ""
            if player_tags:
                tags = sorted(str(tag) for tag in player_tags if str(tag))
                if not tags:
                    return {}
                placeholders = ",".join("?" for _ in tags)
                where_player = f" AND player_tag IN ({placeholders})"
                params.extend(tags)
            cursor.execute(
                f"""
                SELECT
                    player_tag,
                    COUNT(DISTINCT war_id) AS war_events_joined,
                    SUM(attacks_expected) AS attacks_expected,
                    SUM(attacks_used) AS attacks_used,
                    SUM(stars) AS stars,
                    SUM(destruction) AS destruction,
                    SUM(attack_count) AS attack_count,
                    COUNT(DISTINCT CASE WHEN war_type = 'REG' THEN war_id END) AS regular_war_events_joined,
                    SUM(CASE WHEN war_type = 'REG' THEN attacks_expected ELSE 0 END) AS regular_attacks_expected,
                    SUM(CASE WHEN war_type = 'REG' THEN attacks_used ELSE 0 END) AS regular_attacks_used,
                    COUNT(DISTINCT CASE WHEN war_type = 'CWL' THEN war_id END) AS cwl_events_joined,
                    SUM(CASE WHEN war_type = 'CWL' THEN attacks_expected ELSE 0 END) AS cwl_attacks_expected,
                    SUM(CASE WHEN war_type = 'CWL' THEN attacks_used ELSE 0 END) AS cwl_attacks_used
                FROM war_activity
                WHERE end_ts >= ? AND end_ts <= ?{where_player}
                GROUP BY player_tag
                """,
                params,
            )
            rows = cursor.fetchall()
        return {str(r["player_tag"]): dict(r) for r in rows}

    def _load_clan_war_window_counts_by_type(
        self,
        *,
        cycle_start_ts: int,
        cycle_end_ts: int,
        clan_codes: Optional[Set[str]] = None,
    ) -> Dict[str, Dict[str, int]]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            params: list[Any] = [cycle_start_ts, cycle_end_ts]
            where_codes = ""
            if clan_codes:
                codes = sorted(str(code) for code in clan_codes if str(code))
                if not codes:
                    return {}
                placeholders = ",".join("?" for _ in codes)
                where_codes = f" AND clan_code IN ({placeholders})"
                params.extend(codes)
            cursor.execute(
                f"""
                SELECT
                    clan_code,
                    COUNT(DISTINCT CASE WHEN war_type = 'REG' THEN war_id END) AS regular_wars_in_window,
                    COUNT(DISTINCT CASE WHEN war_type = 'CWL' THEN war_id END) AS cwl_wars_in_window
                FROM war_activity
                WHERE end_ts >= ? AND end_ts <= ?{where_codes}
                GROUP BY clan_code
                """,
                params,
            )
            rows = cursor.fetchall()
        return {
            str(r["clan_code"]): {
                "regular": int(r["regular_wars_in_window"] or 0),
                "cwl": int(r["cwl_wars_in_window"] or 0),
            }
            for r in rows
        }

    def _count_raid_weekends_in_window(self, *, cycle_start_ts: int, cycle_end_ts: int) -> int:
        with sqlite3.connect(self.path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(DISTINCT end_ts)
                FROM raid_member_activity
                WHERE end_ts >= ? AND end_ts <= ?
                """,
                (cycle_start_ts, cycle_end_ts),
            )
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    def _load_raid_activity_aggregate(
        self,
        *,
        cycle_start_ts: int,
        cycle_end_ts: int,
        player_tags: Optional[Set[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if player_tags:
                tags = sorted(str(t) for t in player_tags if str(t))
                if not tags:
                    return {}
                placeholders = ",".join("?" for _ in tags)
                cursor.execute(
                    f"""
                    SELECT
                        player_tag,
                        COUNT(DISTINCT CASE WHEN attacks > 0 THEN end_ts END) AS raid_weekends_participated,
                        SUM(attacks) AS attacks,
                        SUM(loot) AS loot
                    FROM raid_member_activity
                    WHERE end_ts >= ? AND end_ts <= ?
                      AND player_tag IN ({placeholders})
                    GROUP BY player_tag
                    """,
                    [cycle_start_ts, cycle_end_ts, *tags],
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        player_tag,
                        COUNT(DISTINCT CASE WHEN attacks > 0 THEN end_ts END) AS raid_weekends_participated,
                        SUM(attacks) AS attacks,
                        SUM(loot) AS loot
                    FROM raid_member_activity
                    WHERE end_ts >= ? AND end_ts <= ?
                    GROUP BY player_tag
                    """,
                    (cycle_start_ts, cycle_end_ts),
                )
            rows = cursor.fetchall()
        return {str(r["player_tag"]): dict(r) for r in rows}

    def _load_snapshot_counter_aggregate(
        self,
        *,
        cycle_start_ts: int,
        cycle_end_ts: int,
        player_tags: Optional[Set[str]] = None,
    ) -> Dict[str, Dict[str, int]]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if player_tags:
                tags = sorted(str(t) for t in player_tags if str(t))
                if not tags:
                    return {}
                placeholders = ",".join("?" for _ in tags)
                cursor.execute(
                    f"""
                    SELECT s.player_tag, s.captured_ts, s.donations, s.donations_received
                    FROM player_snapshots s
                    JOIN (
                        SELECT player_tag, MAX(captured_ts) AS captured_ts
                        FROM player_snapshots
                        WHERE captured_ts <= ? AND player_tag IN ({placeholders})
                        GROUP BY player_tag
                    ) b
                    ON b.player_tag = s.player_tag AND b.captured_ts = s.captured_ts
                    UNION ALL
                    SELECT player_tag, captured_ts, donations, donations_received
                    FROM player_snapshots
                    WHERE captured_ts > ? AND captured_ts <= ? AND player_tag IN ({placeholders})
                    ORDER BY player_tag ASC, captured_ts ASC
                    """,
                    [cycle_start_ts, *tags, cycle_start_ts, cycle_end_ts, *tags],
                )
            else:
                cursor.execute(
                    """
                    SELECT s.player_tag, s.captured_ts, s.donations, s.donations_received
                    FROM player_snapshots s
                    JOIN (
                        SELECT player_tag, MAX(captured_ts) AS captured_ts
                        FROM player_snapshots
                        WHERE captured_ts <= ?
                        GROUP BY player_tag
                    ) b
                    ON b.player_tag = s.player_tag AND b.captured_ts = s.captured_ts
                    UNION ALL
                    SELECT player_tag, captured_ts, donations, donations_received
                    FROM player_snapshots
                    WHERE captured_ts > ? AND captured_ts <= ?
                    ORDER BY player_tag ASC, captured_ts ASC
                    """,
                    (cycle_start_ts, cycle_start_ts, cycle_end_ts),
                )
            points = [dict(row) for row in cursor.fetchall()]

        totals: Dict[str, Dict[str, Any]] = {}
        for point in points:
            tag = str(point.get("player_tag") or "")
            if not tag:
                continue
            ts = int(point.get("captured_ts") or 0)
            donations = max(0, int(point.get("donations") or 0))
            received = max(0, int(point.get("donations_received") or 0))
            bucket = totals.setdefault(
                tag,
                {
                    "donations": 0,
                    "donations_received": 0,
                    "_prev_donations": None,
                    "_prev_received": None,
                    "_has_window_point": False,
                },
            )
            if ts <= cycle_start_ts:
                bucket["_prev_donations"] = donations
                bucket["_prev_received"] = received
                continue

            bucket["_has_window_point"] = True
            prev_donations = bucket.get("_prev_donations")
            prev_received = bucket.get("_prev_received")
            if prev_donations is None:
                bucket["donations"] += donations
            else:
                delta_donations = donations - int(prev_donations)
                bucket["donations"] += donations if delta_donations < 0 else delta_donations

            if prev_received is None:
                bucket["donations_received"] += received
            else:
                delta_received = received - int(prev_received)
                bucket["donations_received"] += received if delta_received < 0 else delta_received

            bucket["_prev_donations"] = donations
            bucket["_prev_received"] = received

        out: Dict[str, Dict[str, int]] = {}
        for tag, values in totals.items():
            if not bool(values.get("_has_window_point")):
                continue
            out[tag] = {
                "donations": int(values.get("donations") or 0),
                "donations_received": int(values.get("donations_received") or 0),
            }
        return out

    def _load_earliest_snapshot_in_window(
        self,
        *,
        cycle_start_ts: int,
        cycle_end_ts: int,
        player_tags: Set[str],
    ) -> Dict[str, Dict[str, Any]]:
        tags = sorted(str(t) for t in player_tags if str(t))
        if not tags:
            return {}
        placeholders = ",".join("?" for _ in tags)
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT s.player_tag, s.townhall, s.hero_sum, s.pet_sum, s.equipment_sum,
                       s.troop_sum, s.spell_sum, s.games_total, s.capital_contrib
                FROM player_snapshots s
                JOIN (
                    SELECT player_tag, MIN(captured_ts) AS min_ts
                    FROM player_snapshots
                    WHERE captured_ts >= ? AND captured_ts <= ? AND player_tag IN ({placeholders})
                    GROUP BY player_tag
                ) m
                ON m.player_tag = s.player_tag AND m.min_ts = s.captured_ts
                """,
                [cycle_start_ts, cycle_end_ts, *tags],
            )
            rows = [dict(r) for r in cursor.fetchall()]
        return {str(r.get("player_tag") or ""): r for r in rows if str(r.get("player_tag") or "")}

    def _load_latest_snapshot_before_or_at(
        self,
        *,
        cutoff_ts: int,
        player_tags: Set[str],
    ) -> Dict[str, Dict[str, Any]]:
        tags = sorted(str(t) for t in player_tags if str(t))
        if not tags:
            return {}
        placeholders = ",".join("?" for _ in tags)
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT s.player_tag, s.townhall, s.hero_sum, s.pet_sum, s.equipment_sum,
                       s.troop_sum, s.spell_sum, s.games_total, s.capital_contrib
                FROM player_snapshots s
                JOIN (
                    SELECT player_tag, MAX(captured_ts) AS max_ts
                    FROM player_snapshots
                    WHERE captured_ts <= ? AND player_tag IN ({placeholders})
                    GROUP BY player_tag
                ) m
                ON m.player_tag = s.player_tag AND m.max_ts = s.captured_ts
                """,
                [cutoff_ts, *tags],
            )
            rows = [dict(r) for r in cursor.fetchall()]
        return {str(r.get("player_tag") or ""): r for r in rows if str(r.get("player_tag") or "")}
