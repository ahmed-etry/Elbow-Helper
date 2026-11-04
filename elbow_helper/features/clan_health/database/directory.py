"""The player directory: each account the bot has seen in a family clan, as last seen."""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any
from typing import Iterable

from elbow_helper.configuration.clans import CLANS


class ClanHealthDirectoryReads:
    def directory_names(self) -> list[dict[str, str]]:
        """Each account's latest name in a family clan, former members included."""
        if not self.path.exists():
            return []
        codes = tuple(CLANS)
        placeholders = ",".join("?" for _ in codes)
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                f"SELECT player_tag, player_name FROM player_directory WHERE clan_code IN ({placeholders}) "
                "ORDER BY player_tag, player_name", codes)]

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
                self._db_connect(timeout=10)
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
