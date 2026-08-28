"""Fast, stale-safe roster lookup for Discord command choices."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from ..config import ROSTER_SEARCH_CACHE_TTL_SECONDS
from ..config import ROSTER_SEARCH_INITIAL_WAIT_SECONDS
from ..repository import RosterRepository
from ..models import Roster


LOGGER = logging.getLogger(__name__)


class RosterSearchCache:
    """Keep roster command choices responsive while SQLite remains authoritative."""

    def __init__(self, repository: RosterRepository):
        self._repository = repository
        self._cache: dict[int, tuple[float, tuple[Roster, ...]]] = {}
        self._refresh_tasks: dict[int, asyncio.Task[None]] = {}
        self._generations: dict[int, int] = {}

    async def warm(self) -> None:
        rows_by_guild: dict[int, list[Roster]] = {}
        for roster in await asyncio.to_thread(self._repository.list_all_rosters):
            rows_by_guild.setdefault(roster.guild_id, []).append(roster)
        for guild_id, rows in rows_by_guild.items():
            self._set(guild_id, rows)

    def close(self) -> None:
        for task in self._refresh_tasks.values():
            if not task.done():
                task.cancel()

    def upsert(self, roster: Roster) -> None:
        cached = self._cache.get(roster.guild_id)
        self._generations[roster.guild_id] = self._generations.get(roster.guild_id, 0) + 1
        if cached is None:
            return
        rows = [row for row in cached[1] if row.id != roster.id]
        rows.append(roster)
        self._set(roster.guild_id, rows)

    def remove(self, roster: Roster) -> None:
        cached = self._cache.get(roster.guild_id)
        self._generations[roster.guild_id] = self._generations.get(roster.guild_id, 0) + 1
        if cached is None:
            return
        self._set(
            roster.guild_id,
            [row for row in cached[1] if row.id != roster.id],
        )

    async def rows(self, guild_id: int) -> tuple[Roster, ...]:
        cached = self._cache.get(guild_id)
        if cached is not None:
            cached_at, rows = cached
            if time.monotonic() - cached_at >= ROSTER_SEARCH_CACHE_TTL_SECONDS:
                self._start_refresh(guild_id)
            return rows

        task = self._start_refresh(guild_id)
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=ROSTER_SEARCH_INITIAL_WAIT_SECONDS,
            )
        except TimeoutError:
            LOGGER.warning("Initial roster search refresh timed out guild_id=%s", guild_id)
        cached = self._cache.get(guild_id)
        return cached[1] if cached is not None else ()

    def _set(
        self,
        guild_id: int,
        rows: list[Roster] | tuple[Roster, ...],
    ) -> None:
        ordered = tuple(sorted(rows, key=lambda row: (row.name.casefold(), row.id)))
        self._cache[guild_id] = (time.monotonic(), ordered)

    def _start_refresh(self, guild_id: int) -> asyncio.Task[None]:
        existing = self._refresh_tasks.get(guild_id)
        if existing is not None and not existing.done():
            return existing
        generation = self._generations.get(guild_id, 0)
        task = asyncio.create_task(self._refresh(guild_id, generation))
        self._refresh_tasks[guild_id] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if self._refresh_tasks.get(guild_id) is completed:
                self._refresh_tasks.pop(guild_id, None)

        task.add_done_callback(discard)
        return task

    async def _refresh(self, guild_id: int, generation: int) -> None:
        try:
            rows = await asyncio.to_thread(self._repository.list_rosters, guild_id)
        except (OSError, RuntimeError, sqlite3.Error):
            LOGGER.exception("Roster search refresh failed guild_id=%s", guild_id)
            return
        if self._generations.get(guild_id, 0) != generation:
            return
        self._set(guild_id, rows)
