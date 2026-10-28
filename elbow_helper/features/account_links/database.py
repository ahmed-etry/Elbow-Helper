"""SQLite storage for player links and pending suggestions."""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Any, Iterable, Iterator, Mapping

from elbow_helper.data.link_facade import LinkFacade
from elbow_helper.data.store.legacy_links import LinkConflict

from .config import DB_PATH
from .schema import DATABASE


class AccountLinksDbMixin:
    @contextmanager
    def _db_connect(self) -> Iterator[sqlite3.Connection]:
        service = getattr(self, "link_service", None)
        conn = (sqlite3.connect(service.legacy_path.resolve().as_uri() + "?mode=rw", uri=True)
                if service is not None else DATABASE.connect(DB_PATH))
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        DATABASE.initialize(DB_PATH)

    def _links(self) -> LinkFacade:
        return LinkFacade(getattr(self, "link_service", None))

    def _link_writer(self):
        service = getattr(self, "link_service", None)
        if service is None:
            raise LinkConflict("links_topic_unavailable")
        return service

    def get_all_links(self) -> dict[str, dict[str, Any]]:
        return self._links().all()

    def get_link_by_tag(self, player_tag: str) -> dict[str, Any] | None:
        return self._links().by_tag(player_tag)

    def get_links_by_tags(self, player_tags: Iterable[str]) -> dict[str, dict[str, Any] | None]:
        return self._links().by_tags(player_tags)

    def get_links_for_user(self, discord_user_id: int) -> list[dict[str, Any]]:
        return self._links().for_user(discord_user_id)

    def get_links_for_members(self, member_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
        return self._links().for_members(member_ids)

    async def delete_link(self, player_tag: str) -> None:
        await self._link_writer().delete_link(player_tag)

    async def delete_links(self, player_tags: Iterable[str]) -> None:
        await self._link_writer().delete_links(player_tags)

    async def upsert_link(self, **row) -> None:
        await self._link_writer().upsert_link(**row)

    async def upsert_links(self, links: Iterable[Mapping[str, object]]) -> None:
        await self._link_writer().upsert_links(links)

    async def restore_links(self, before: Mapping[str, Mapping[str, Any] | None]) -> None:
        await self._link_writer().restore_links(before)

    async def update_link_last_seen(self, **row) -> None:
        await self._link_writer().update_link_last_seen(**row)

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
