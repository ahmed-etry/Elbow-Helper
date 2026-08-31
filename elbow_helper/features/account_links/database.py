"""SQLite storage for player links and pending suggestions."""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Any, Iterable, Iterator, Mapping

from .config import DB_PATH


_LINK_UPSERT_SQL = """
    INSERT INTO links (
        player_tag,
        discord_user_id,
        is_primary,
        player_name_last_seen,
        last_seen_clan_tag,
        last_seen_clan_code,
        last_seen_role
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(player_tag) DO UPDATE SET
        discord_user_id = excluded.discord_user_id,
        is_primary = excluded.is_primary,
        player_name_last_seen = excluded.player_name_last_seen,
        last_seen_clan_tag = excluded.last_seen_clan_tag,
        last_seen_clan_code = excluded.last_seen_clan_code,
        last_seen_role = excluded.last_seen_role
"""


class AccountLinksDbMixin:
    @contextmanager
    def _db_connect(self) -> Iterator[sqlite3.Connection]:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db_connect() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS links (
                    player_tag TEXT PRIMARY KEY,
                    discord_user_id INTEGER NOT NULL,
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    player_name_last_seen TEXT DEFAULT '',
                    last_seen_clan_tag TEXT DEFAULT '',
                    last_seen_clan_code TEXT DEFAULT '',
                    last_seen_role TEXT DEFAULT ''
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS suggestions (
                    player_tag TEXT PRIMARY KEY,
                    player_name TEXT NOT NULL,
                    current_clan_code TEXT NOT NULL,
                    current_clan_tag TEXT NOT NULL,
                    proposed_discord_user_id INTEGER NOT NULL,
                    proposed_display_name TEXT DEFAULT '',
                    review_channel_id INTEGER DEFAULT 0,
                    review_message_id INTEGER DEFAULT 0
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ignored_tags (
                    player_tag TEXT PRIMARY KEY
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_discord_user ON links(discord_user_id)")
            conn.commit()

    def get_all_links(self) -> dict[str, dict[str, Any]]:
        with self._db_connect() as conn:
            rows = conn.execute("SELECT * FROM links").fetchall()
        return {str(row["player_tag"]): dict(row) for row in rows}

    def get_link_by_tag(self, player_tag: str) -> dict[str, Any] | None:
        with self._db_connect() as conn:
            row = conn.execute("SELECT * FROM links WHERE player_tag = ?", (player_tag,)).fetchone()
        return dict(row) if row else None

    def get_links_for_user(self, discord_user_id: int) -> list[dict[str, Any]]:
        with self._db_connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM links
                WHERE discord_user_id = ?
                ORDER BY
                    is_primary DESC,
                    COALESCE(player_name_last_seen, '') COLLATE NOCASE ASC,
                    player_tag ASC
                """,
                (int(discord_user_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_link(self, player_tag: str) -> None:
        self.delete_links([player_tag])

    def delete_links(self, player_tags: Iterable[str]) -> None:
        values = [(player_tag,) for player_tag in player_tags]
        if not values:
            return
        with self._db_connect() as conn, conn:
            conn.executemany("DELETE FROM links WHERE player_tag = ?", values)

    def upsert_link(
        self,
        *,
        player_tag: str,
        discord_user_id: int,
        is_primary: bool = False,
        player_name_last_seen: str = "",
        last_seen_clan_tag: str = "",
        last_seen_clan_code: str = "",
        last_seen_role: str = "",
    ) -> None:
        self.upsert_links(
            [
                {
                    "player_tag": player_tag,
                    "discord_user_id": discord_user_id,
                    "is_primary": is_primary,
                    "player_name_last_seen": player_name_last_seen,
                    "last_seen_clan_tag": last_seen_clan_tag,
                    "last_seen_clan_code": last_seen_clan_code,
                    "last_seen_role": last_seen_role,
                }
            ]
        )

    def upsert_links(self, links: Iterable[Mapping[str, object]]) -> None:
        values = [
            (
                str(link["player_tag"]),
                int(link["discord_user_id"]),
                1 if bool(link.get("is_primary")) else 0,
                str(link.get("player_name_last_seen") or ""),
                str(link.get("last_seen_clan_tag") or ""),
                str(link.get("last_seen_clan_code") or ""),
                str(link.get("last_seen_role") or ""),
            )
            for link in links
        ]
        if not values:
            return
        with self._db_connect() as conn, conn:
            conn.executemany(_LINK_UPSERT_SQL, values)

    def update_link_last_seen(
        self,
        *,
        player_tag: str,
        player_name_last_seen: str,
        last_seen_clan_tag: str,
        last_seen_clan_code: str,
        last_seen_role: str,
    ) -> None:
        with self._db_connect() as conn:
            conn.execute(
                """
                UPDATE links
                SET player_name_last_seen = ?,
                    last_seen_clan_tag = ?,
                    last_seen_clan_code = ?,
                    last_seen_role = ?
                WHERE player_tag = ?
                """,
                (
                    player_name_last_seen,
                    last_seen_clan_tag,
                    last_seen_clan_code,
                    last_seen_role,
                    player_tag,
                ),
            )
            conn.commit()

    def delete_suggestion(self, player_tag: str) -> None:
        with self._db_connect() as conn:
            conn.execute("DELETE FROM suggestions WHERE player_tag = ?", (player_tag,))
            conn.commit()

    def get_pending_suggestion(self, player_tag: str) -> dict[str, Any] | None:
        with self._db_connect() as conn:
            row = conn.execute("SELECT * FROM suggestions WHERE player_tag = ?", (player_tag,)).fetchone()
        return dict(row) if row else None

    def list_pending_suggestions(self) -> list[dict[str, Any]]:
        with self._db_connect() as conn:
            rows = conn.execute("SELECT * FROM suggestions ORDER BY player_tag ASC").fetchall()
        return [dict(row) for row in rows]

    def upsert_suggestion(
        self,
        *,
        player_tag: str,
        player_name: str,
        current_clan_code: str,
        current_clan_tag: str,
        proposed_discord_user_id: int,
        proposed_display_name: str,
        review_channel_id: int,
        review_message_id: int,
    ) -> None:
        with self._db_connect() as conn:
            conn.execute(
                """
                INSERT INTO suggestions (
                    player_tag,
                    player_name,
                    current_clan_code,
                    current_clan_tag,
                    proposed_discord_user_id,
                    proposed_display_name,
                    review_channel_id,
                    review_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_tag) DO UPDATE SET
                    player_name = excluded.player_name,
                    current_clan_code = excluded.current_clan_code,
                    current_clan_tag = excluded.current_clan_tag,
                    proposed_discord_user_id = excluded.proposed_discord_user_id,
                    proposed_display_name = excluded.proposed_display_name,
                    review_channel_id = excluded.review_channel_id,
                    review_message_id = excluded.review_message_id
                """,
                (
                    player_tag,
                    player_name,
                    current_clan_code,
                    current_clan_tag,
                    int(proposed_discord_user_id),
                    proposed_display_name,
                    int(review_channel_id),
                    int(review_message_id),
                ),
            )
            conn.commit()

    def is_ignored_tag(self, player_tag: str) -> bool:
        with self._db_connect() as conn:
            row = conn.execute("SELECT 1 FROM ignored_tags WHERE player_tag = ?", (player_tag,)).fetchone()
        return row is not None

    def add_ignored_tag(self, player_tag: str) -> None:
        with self._db_connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ignored_tags(player_tag) VALUES (?)",
                (player_tag,),
            )
            conn.commit()
