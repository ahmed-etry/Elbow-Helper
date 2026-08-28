"""Supported CWL history reads from Clan Health storage."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import sqlite3
from typing import Any
from typing import Iterable


class ClanHealthCwlReads:
    """Expose completed CWL data without leaking the database path or SQL."""

    def bonus_seasons(
        self,
        clan_codes: list[str] | None = None,
    ) -> list[str]:
        if not self.path.exists():
            return []
        params: list[Any] = []
        clan_filter = ""
        if clan_codes:
            codes = [str(code) for code in clan_codes if str(code)]
            if codes:
                placeholders = ",".join("?" for _ in codes)
                clan_filter = f" AND clan_code IN ({placeholders})"
                params.extend(codes)
        try:
            with closing(
                sqlite3.connect(self.path, timeout=5)
            ) as connection:
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT cwl_season
                    FROM wars
                    WHERE war_type = 'CWL'
                      AND state = 'warEnded'
                      AND cwl_season != ''
                      {clan_filter}
                    ORDER BY cwl_season DESC
                    """,
                    params,
                ).fetchall()
        except sqlite3.Error:
            return []
        return [str(row[0]) for row in rows if row and row[0]]

    def bonus_wars(
        self,
        clan_code: str,
        season: str,
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise sqlite3.OperationalError(
                "clan database does not exist"
            )
        with closing(
            sqlite3.connect(self.path, timeout=30)
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            wars = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        war_id, clan_code, cwl_season, cwl_round,
                        attacks_per_member, state, end_ts
                    FROM wars
                    WHERE war_type = 'CWL'
                      AND clan_code = ?
                      AND cwl_season = ?
                      AND state = 'warEnded'
                    ORDER BY cwl_round ASC, end_ts ASC, war_id ASC
                    """,
                    (clan_code, season),
                ).fetchall()
            ]
            if not wars:
                return []

            war_ids = [str(row["war_id"]) for row in wars]
            placeholders = ",".join("?" for _ in war_ids)
            roster_by_war: dict[str, list[dict[str, Any]]] = {}
            for row in connection.execute(
                f"""
                SELECT
                    war_id, player_tag, player_name, townhall,
                    map_position, attacks_expected, attacks_used,
                    roster_state
                FROM war_roster_members
                WHERE clan_code = ? AND war_id IN ({placeholders})
                ORDER BY war_id, map_position, player_name
                """,
                [clan_code, *war_ids],
            ).fetchall():
                roster_by_war.setdefault(
                    str(row["war_id"]),
                    [],
                ).append(dict(row))

            attacks_by_war: dict[str, list[dict[str, Any]]] = {}
            for row in connection.execute(
                f"""
                SELECT
                    war_id, player_tag, player_name, attack_order,
                    defender_tag, defender_townhall, stars, destruction
                FROM war_attacks
                WHERE clan_code = ?
                  AND war_type = 'CWL'
                  AND war_id IN ({placeholders})
                ORDER BY war_id, attack_order, player_name
                """,
                [clan_code, *war_ids],
            ).fetchall():
                attacks_by_war.setdefault(
                    str(row["war_id"]),
                    [],
                ).append(dict(row))

        for war in wars:
            war_id = str(war["war_id"])
            war["roster"] = roster_by_war.get(war_id, [])
            war["attacks"] = attacks_by_war.get(war_id, [])
        return wars

    @staticmethod
    def _empty_roster_history() -> dict[str, list[dict[str, Any]]]:
        return {
            "seasons": [],
            "wars": [],
            "roster": [],
            "attacks": [],
        }

    def roster_history(
        self,
        history_limit: int | None,
    ) -> dict[str, list[dict[str, Any]]]:
        if not self.path.exists():
            return self._empty_roster_history()
        with closing(
            sqlite3.connect(self.path, timeout=30)
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            completed_rows = connection.execute(
                """
                SELECT
                    cwl_season,
                    clan_code,
                    MAX(end_ts) AS latest_end_ts
                FROM wars
                WHERE war_type = 'CWL'
                  AND state = 'warEnded'
                  AND cwl_season != ''
                GROUP BY cwl_season, clan_code
                HAVING COUNT(DISTINCT war_id) >= 7
                """
            ).fetchall()
            groups_by_clan: dict[str, list[sqlite3.Row]] = defaultdict(list)
            for row in completed_rows:
                groups_by_clan[str(row["clan_code"])].append(row)

            selected_rows: list[sqlite3.Row] = []
            for rows in groups_by_clan.values():
                ordered = sorted(
                    rows,
                    key=lambda row: (
                        int(row["latest_end_ts"] or 0),
                        str(row["cwl_season"]),
                    ),
                    reverse=True,
                )
                if history_limit is not None:
                    ordered = ordered[: max(1, int(history_limit))]
                selected_rows.extend(ordered)

            completed_groups = {
                (str(row["cwl_season"]), str(row["clan_code"]))
                for row in selected_rows
            }
            latest_by_season: dict[str, int] = {}
            for row in selected_rows:
                season = str(row["cwl_season"])
                latest_by_season[season] = max(
                    latest_by_season.get(season, 0),
                    int(row["latest_end_ts"] or 0),
                )
            seasons = [
                {"key": season, "latest_end_ts": latest_end_ts}
                for season, latest_end_ts in sorted(
                    latest_by_season.items(),
                    key=lambda item: (item[1], item[0]),
                    reverse=True,
                )
            ]
            if not seasons:
                return self._empty_roster_history()

            season_keys = [row["key"] for row in seasons]
            season_placeholders = ",".join("?" for _ in season_keys)
            wars = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT *
                    FROM wars
                    WHERE war_type = 'CWL'
                      AND state = 'warEnded'
                      AND cwl_season IN ({season_placeholders})
                    ORDER BY end_ts, clan_code, cwl_round, war_id
                    """,
                    season_keys,
                ).fetchall()
                if (
                    str(row["cwl_season"]),
                    str(row["clan_code"]),
                ) in completed_groups
            ]
            if not wars:
                return {
                    "seasons": seasons,
                    "wars": [],
                    "roster": [],
                    "attacks": [],
                }

            war_keys = {
                (str(row["war_id"]), str(row["clan_code"]))
                for row in wars
            }
            war_ids = sorted({war_id for war_id, _ in war_keys})
            clan_codes = sorted({clan_code for _, clan_code in war_keys})
            war_placeholders = ",".join("?" for _ in war_ids)
            clan_placeholders = ",".join("?" for _ in clan_codes)
            params = [*war_ids, *clan_codes]
            roster = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT *
                    FROM war_roster_members
                    WHERE war_id IN ({war_placeholders})
                      AND clan_code IN ({clan_placeholders})
                    ORDER BY clan_code, war_id, map_position, player_name
                    """,
                    params,
                ).fetchall()
                if (
                    str(row["war_id"]),
                    str(row["clan_code"]),
                ) in war_keys
            ]
            attacks = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT *
                    FROM war_attacks
                    WHERE war_type = 'CWL'
                      AND war_id IN ({war_placeholders})
                      AND clan_code IN ({clan_placeholders})
                    ORDER BY clan_code, war_id, attack_order, player_name
                    """,
                    params,
                ).fetchall()
                if (
                    str(row["war_id"]),
                    str(row["clan_code"]),
                ) in war_keys
            ]
        return {
            "seasons": seasons,
            "wars": wars,
            "roster": roster,
            "attacks": attacks,
        }

    def directory_players(
        self,
        player_tags: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        tags = sorted({str(tag) for tag in player_tags if str(tag)})
        if not tags or not self.path.exists():
            return {}
        placeholders = ",".join("?" for _ in tags)
        try:
            with closing(
                sqlite3.connect(self.path, timeout=10)
            ) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    f"""
                    SELECT
                        player_tag, player_name, clan_code,
                        townhall, last_seen_ts
                    FROM player_directory
                    WHERE player_tag IN ({placeholders})
                    """,
                    tags,
                ).fetchall()
        except sqlite3.Error:
            return {}
        return {
            str(row["player_tag"]): dict(row)
            for row in rows
        }
