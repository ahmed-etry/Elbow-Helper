
"""Stored report and snapshot write helpers for clan health."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from ..models import GOOD
from ..models import normalize_player_verdict

class ClanHealthRecords:
    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        return int(value)

    def _store_snapshots(self, captured_ts: int, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT OR REPLACE INTO player_snapshots (
                    captured_ts, clan_code, player_tag, player_name, townhall,
                    donations, donations_received, trophies, war_stars, attack_wins,
                    capital_contrib, hero_sum, pet_sum, equipment_sum, troop_sum, spell_sum, games_total
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        captured_ts,
                        row["clan_code"],
                        row["player_tag"],
                        row["player_name"],
                        self._optional_int(row.get("townhall")),
                        int(row.get("donations") or 0),
                        int(row.get("donations_received") or 0),
                        int(row.get("trophies") or 0),
                        self._optional_int(row.get("war_stars")),
                        self._optional_int(row.get("attack_wins")),
                        self._optional_int(row.get("capital_contrib")),
                        self._optional_int(row.get("hero_sum")),
                        self._optional_int(row.get("pet_sum")),
                        self._optional_int(row.get("equipment_sum")),
                        self._optional_int(row.get("troop_sum")),
                        self._optional_int(row.get("spell_sum")),
                        self._optional_int(row.get("games_total")),
                    )
                    for row in rows
                ],
            )
            cursor.executemany(
                """
                INSERT INTO player_directory (
                    player_tag,
                    player_name,
                    clan_code,
                    townhall,
                    player_name_search,
                    player_tag_search,
                    clan_code_search,
                    first_seen_ts,
                    last_seen_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_tag) DO UPDATE SET
                    player_name = CASE
                        WHEN excluded.last_seen_ts >= player_directory.last_seen_ts
                        THEN excluded.player_name
                        ELSE player_directory.player_name
                    END,
                    clan_code = CASE
                        WHEN excluded.last_seen_ts >= player_directory.last_seen_ts
                        THEN excluded.clan_code
                        ELSE player_directory.clan_code
                    END,
                    townhall = CASE
                        WHEN excluded.last_seen_ts >= player_directory.last_seen_ts
                        THEN excluded.townhall
                        ELSE player_directory.townhall
                    END,
                    player_name_search = CASE
                        WHEN excluded.last_seen_ts >= player_directory.last_seen_ts
                        THEN excluded.player_name_search
                        ELSE player_directory.player_name_search
                    END,
                    player_tag_search = excluded.player_tag_search,
                    clan_code_search = CASE
                        WHEN excluded.last_seen_ts >= player_directory.last_seen_ts
                        THEN excluded.clan_code_search
                        ELSE player_directory.clan_code_search
                    END,
                    first_seen_ts = MIN(player_directory.first_seen_ts, excluded.first_seen_ts),
                    last_seen_ts = MAX(player_directory.last_seen_ts, excluded.last_seen_ts)
                """,
                [
                    (
                        str(row["player_tag"]),
                        str(row["player_name"]),
                        str(row["clan_code"]),
                        int(row.get("townhall") or 0),
                        str(row["player_name"]).lower(),
                        str(row["player_tag"]).replace("#", "").lower(),
                        str(row["clan_code"]).lower(),
                        captured_ts,
                        captured_ts,
                    )
                    for row in rows
                ],
            )
            # PK(captured_ts, clan_code, player_tag) keeps one row per capture tick.
            conn.commit()

    def _store_report(
        self,
        *,
        run_id: str,
        created_ts: int,
        season_key: str,
        scope: str,
        partial: bool,
        cycle_start_ts: int,
        cycle_end_ts: int,
        rows: List[Dict[str, Any]],
    ) -> None:
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            cursor = conn.cursor()
            # Keep run metadata + player rows in one transaction.
            cursor.execute(
                """
                INSERT OR REPLACE INTO report_runs (
                    run_id, created_ts, season_key, scope, partial, cycle_start_ts, cycle_end_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, created_ts, season_key, scope, 1 if partial else 0, cycle_start_ts, cycle_end_ts),
            )
            cursor.executemany(
                """
                INSERT OR REPLACE INTO report_players (
                    run_id, season_key, clan_code, player_tag, player_name, status, flags_json, note,
                    war_hits_used, war_hits_expected, war_missed, war_stars_total, war_destruction_total, war_attack_count,
                    raid_attacks, raid_expected, raid_loot, raid_expected_estimated, donations, donations_received,
                    trophies, war_stars, attack_wins, capital_contrib, townhall, hero_sum,
                    pet_sum, equipment_sum, troop_sum, spell_sum, games_total,
                    hero_delta, pet_delta, equipment_delta, troop_delta, spell_delta, capital_delta, th_delta, games_delta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        season_key,
                        row["clan_code"],
                        row["player_tag"],
                        row["player_name"],
                        normalize_player_verdict(row.get("status", GOOD)),
                        json.dumps(row.get("flags", []), ensure_ascii=False),
                        row.get("note", ""),
                        int(row.get("war_hits_used") or 0),
                        int(row.get("war_hits_expected") or 0),
                        int(row.get("war_missed") or 0),
                        float(row.get("war_stars_total") or 0.0),
                        float(row.get("war_destruction_total") or 0.0),
                        int(row.get("war_attack_count") or 0),
                        int(row.get("raid_attacks") or 0),
                        int(row.get("raid_expected") or 0),
                        int(row.get("raid_loot") or 0),
                        1 if row.get("raid_expected_estimated") else 0,
                        int(row.get("donations") or 0),
                        int(row.get("donations_received") or 0),
                        int(row.get("trophies") or 0),
                        self._optional_int(row.get("war_stars")),
                        self._optional_int(row.get("attack_wins")),
                        self._optional_int(row.get("capital_contrib")),
                        self._optional_int(row.get("townhall")),
                        self._optional_int(row.get("hero_sum")),
                        self._optional_int(row.get("pet_sum")),
                        self._optional_int(row.get("equipment_sum")),
                        self._optional_int(row.get("troop_sum")),
                        self._optional_int(row.get("spell_sum")),
                        self._optional_int(row.get("games_total")),
                        row.get("hero_delta"),
                        row.get("pet_delta"),
                        row.get("equipment_delta"),
                        row.get("troop_delta"),
                        row.get("spell_delta"),
                        row.get("capital_delta"),
                        row.get("th_delta"),
                        row.get("games_delta"),
                    )
                    for row in rows
                ],
            )
            conn.commit()

    def _store_war_activity_rows(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT OR REPLACE INTO war_activity (
                    war_id, war_type, clan_code, clan_tag, end_ts, player_tag, player_name,
                    attacks_expected, attacks_used, stars, destruction, attack_count, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(r.get("war_id") or ""),
                        str(r.get("war_type") or "UNKNOWN"),
                        str(r.get("clan_code") or ""),
                        str(r.get("clan_tag") or ""),
                        int(r.get("end_ts") or 0),
                        str(r.get("player_tag") or ""),
                        str(r.get("player_name") or ""),
                        int(r.get("attacks_expected") or 0),
                        int(r.get("attacks_used") or 0),
                        float(r.get("stars") or 0.0),
                        float(r.get("destruction") or 0.0),
                        int(r.get("attack_count") or 0),
                        str(r.get("source") or ""),
                    )
                    for r in rows
                    if r.get("war_id") and r.get("player_tag")
                ],
            )
            conn.commit()
        # Attempted row count (upserts may overwrite existing PKs).
        return len(rows)

    def _store_war_rows(self, rows: List[Dict[str, Any]]) -> int:
        valid_rows = [r for r in rows if r.get("war_id") and r.get("clan_code")]
        if not valid_rows:
            return 0
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.executemany(
                """
                INSERT INTO wars (
                    war_id, war_type, clan_code, clan_tag,
                    opponent_tag, opponent_name,
                    cwl_season, cwl_league, cwl_round,
                    team_size, attacks_per_member, state,
                    preparation_start_ts, start_ts, end_ts,
                    last_seen_ts, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(war_id, clan_code) DO UPDATE SET
                    war_type = excluded.war_type,
                    clan_tag = excluded.clan_tag,
                    opponent_tag = CASE
                        WHEN excluded.opponent_tag != '' THEN excluded.opponent_tag
                        ELSE wars.opponent_tag
                    END,
                    opponent_name = CASE
                        WHEN excluded.opponent_name != '' THEN excluded.opponent_name
                        ELSE wars.opponent_name
                    END,
                    cwl_season = CASE
                        WHEN excluded.cwl_season != '' THEN excluded.cwl_season
                        ELSE wars.cwl_season
                    END,
                    cwl_league = CASE
                        WHEN excluded.cwl_league != '' THEN excluded.cwl_league
                        ELSE wars.cwl_league
                    END,
                    cwl_round = CASE
                        WHEN excluded.cwl_round > 0 THEN excluded.cwl_round
                        ELSE wars.cwl_round
                    END,
                    team_size = CASE
                        WHEN excluded.team_size > 0 THEN excluded.team_size
                        ELSE wars.team_size
                    END,
                    attacks_per_member = CASE
                        WHEN excluded.attacks_per_member > 0 THEN excluded.attacks_per_member
                        ELSE wars.attacks_per_member
                    END,
                    state = excluded.state,
                    preparation_start_ts = CASE
                        WHEN excluded.preparation_start_ts > 0 THEN excluded.preparation_start_ts
                        ELSE wars.preparation_start_ts
                    END,
                    start_ts = CASE
                        WHEN excluded.start_ts > 0 THEN excluded.start_ts
                        ELSE wars.start_ts
                    END,
                    end_ts = CASE
                        WHEN excluded.end_ts > 0 THEN excluded.end_ts
                        ELSE wars.end_ts
                    END,
                    last_seen_ts = MAX(wars.last_seen_ts, excluded.last_seen_ts),
                    source = excluded.source
                """,
                [
                    (
                        str(r.get("war_id") or ""),
                        str(r.get("war_type") or "UNKNOWN"),
                        str(r.get("clan_code") or ""),
                        str(r.get("clan_tag") or ""),
                        str(r.get("opponent_tag") or ""),
                        str(r.get("opponent_name") or ""),
                        str(r.get("cwl_season") or ""),
                        str(r.get("cwl_league") or ""),
                        int(r.get("cwl_round") or 0),
                        int(r.get("team_size") or 0),
                        int(r.get("attacks_per_member") or 0),
                        str(r.get("state") or ""),
                        int(r.get("preparation_start_ts") or 0),
                        int(r.get("start_ts") or 0),
                        int(r.get("end_ts") or 0),
                        int(r.get("last_seen_ts") or 0),
                        str(r.get("source") or ""),
                    )
                    for r in valid_rows
                ],
            )
            conn.commit()
        return len(valid_rows)

    def _store_final_war_roster_rows(self, rows: List[Dict[str, Any]]) -> int:
        valid_rows = [
            r
            for r in rows
            if r.get("war_id")
            and r.get("clan_code")
            and r.get("player_tag")
            and str(r.get("roster_state") or "") in {"inWar", "warEnded"}
        ]
        if not valid_rows:
            return 0

        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for row in valid_rows:
            key = (str(row["war_id"]), str(row["clan_code"]))
            grouped.setdefault(key, []).append(row)

        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            cursor = conn.cursor()
            for (war_id, clan_code), roster_rows in grouped.items():
                cursor.execute(
                    "DELETE FROM war_roster_members WHERE war_id = ? AND clan_code = ?",
                    (war_id, clan_code),
                )
                cursor.executemany(
                    """
                    INSERT INTO war_roster_members (
                        war_id, clan_code, player_tag, player_name,
                        townhall, map_position,
                        attacks_expected, attacks_used,
                        roster_state, captured_ts, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            war_id,
                            clan_code,
                            str(r.get("player_tag") or ""),
                            str(r.get("player_name") or ""),
                            int(r.get("townhall") or 0),
                            int(r.get("map_position") or 0),
                            int(r.get("attacks_expected") or 0),
                            int(r.get("attacks_used") or 0),
                            str(r.get("roster_state") or ""),
                            int(r.get("captured_ts") or 0),
                            str(r.get("source") or ""),
                        )
                        for r in roster_rows
                    ],
                )
            conn.commit()
        return len(valid_rows)

    def _store_war_attack_rows(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT OR REPLACE INTO war_attacks (
                    war_id, war_type, clan_code, clan_tag, end_ts, war_state,
                    player_tag, player_name, attack_order,
                    defender_tag, defender_name, defender_map_position, defender_townhall,
                    stars, destruction, fresh_attack, duration, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(r.get("war_id") or ""),
                        str(r.get("war_type") or ""),
                        str(r.get("clan_code") or ""),
                        str(r.get("clan_tag") or ""),
                        int(r.get("end_ts") or 0),
                        str(r.get("war_state") or ""),
                        str(r.get("player_tag") or ""),
                        str(r.get("player_name") or ""),
                        int(r.get("attack_order") or 0),
                        str(r.get("defender_tag") or ""),
                        str(r.get("defender_name") or ""),
                        int(r.get("defender_map_position") or 0),
                        int(r.get("defender_townhall") or 0),
                        int(r.get("stars") or 0),
                        float(r.get("destruction") or 0.0),
                        1 if r.get("fresh_attack") else 0,
                        int(r.get("duration") or 0),
                        str(r.get("source") or ""),
                    )
                    for r in rows
                    if r.get("war_id") and r.get("player_tag")
                ],
            )
            conn.commit()
        # Attempted row count (upserts may overwrite existing PKs).
        return len(rows)

    def _store_raid_member_activity_rows(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT OR REPLACE INTO raid_member_activity (
                    weekend_id, clan_code, clan_tag, end_ts, player_tag, player_name,
                    attacks, attack_limit, bonus_attack_limit, attacks_expected, loot, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(r.get("weekend_id") or ""),
                        str(r.get("clan_code") or ""),
                        str(r.get("clan_tag") or ""),
                        int(r.get("end_ts") or 0),
                        str(r.get("player_tag") or ""),
                        str(r.get("player_name") or ""),
                        int(r.get("attacks") or 0),
                        int(r.get("attack_limit") or 0),
                        int(r.get("bonus_attack_limit") or 0),
                        int(r.get("attacks_expected") or 0),
                        int(r.get("loot") or 0),
                        str(r.get("source") or ""),
                    )
                    for r in rows
                    if r.get("weekend_id") and r.get("player_tag")
                ],
            )
            conn.commit()
        # Attempted row count (upserts may overwrite existing PKs).
        return len(rows)

    def _load_latest_stored_report_before_ts(
        self,
        *,
        cycle_end_ts: int,
        selected_clans: List[str],
    ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT run_id, created_ts, season_key, partial, cycle_start_ts, cycle_end_ts
                FROM report_runs
                WHERE created_ts <= ?
                ORDER BY created_ts DESC
                """,
                (int(cycle_end_ts),),
            )
            runs = cursor.fetchall()
            if not runs:
                return None, []
            for run in runs:
                placeholders = ",".join("?" for _ in selected_clans)
                cursor.execute(
                    f"""
                    SELECT *
                    FROM report_players
                    WHERE run_id = ? AND clan_code IN ({placeholders})
                    ORDER BY clan_code, player_name
                    """,
                    [run["run_id"], *selected_clans],
                )
                rows = cursor.fetchall()
                if rows:
                    present_codes = {str(row["clan_code"]) for row in rows}
                    required_codes = set(selected_clans)
                    if not required_codes.issubset(present_codes):
                        continue
                    return dict(run), [dict(row) for row in rows]
        return None, []

    def _load_latest_player_report_row(self, season_key: str, player_tag: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT rp.*, rr.created_ts, rr.partial
                FROM report_players rp
                JOIN report_runs rr ON rr.run_id = rp.run_id
                WHERE rp.season_key = ? AND rp.player_tag = ?
                ORDER BY rr.created_ts DESC
                LIMIT 1
                """,
                (season_key, player_tag),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
