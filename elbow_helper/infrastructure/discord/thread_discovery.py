"""Bounded Discord thread discovery transport."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import discord


class DiscordThreadDiscoveryError(RuntimeError):
    """Discord returned thread metadata outside the requested parent/type."""


@dataclass(frozen=True, slots=True)
class DiscordArchivedThreadPage:
    threads: tuple[Any, ...]
    scanned_threads: int
    next_before_id: int | None
    endpoint_exhausted: bool
    mode: str


class DiscordThreadDiscovery:
    """Read active and one bounded archived-thread page through discord.py."""

    async def active_threads(self, guild: discord.Guild) -> tuple[Any, ...]:
        return tuple(await guild.active_threads())

    async def archived_page(
        self,
        parent: discord.TextChannel | discord.ForumChannel,
        *,
        private: bool,
        joined: bool,
        before_id: int | None,
        limit: int,
    ) -> DiscordArchivedThreadPage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid archived thread page size")
        if joined and not private:
            raise ValueError("Joined archive discovery applies only to private threads")
        before = None
        if before_id is not None:
            if type(before_id) is not int or before_id <= 0:
                raise ValueError("Invalid archived thread cursor")
            before = (
                discord.Object(id=before_id)
                if joined else discord.utils.snowflake_time(before_id)
            )
        arguments: dict[str, Any] = {"limit": limit, "before": before}
        if isinstance(parent, discord.TextChannel):
            arguments.update(private=private, joined=joined)
        elif private:
            raise ValueError("That parent does not support private threads")
        threads = tuple([
            thread async for thread in parent.archived_threads(**arguments)
        ])
        for thread in threads:
            if (
                getattr(thread, "parent_id", None) != parent.id
                or bool(thread.is_private()) != private
                or not bool(getattr(thread, "archived", False))
            ):
                raise DiscordThreadDiscoveryError(
                    "Discord archived thread page escaped its requested scope"
                )
        next_before_id = None
        if len(threads) == limit:
            last = threads[-1]
            if joined:
                next_before_id = last.id
            else:
                archived_at = getattr(last, "archive_timestamp", None)
                if not isinstance(archived_at, datetime):
                    raise DiscordThreadDiscoveryError(
                        "Discord archived thread omitted its archive timestamp"
                    )
                next_before_id = discord.utils.time_snowflake(
                    archived_at, high=False,
                )
        return DiscordArchivedThreadPage(
            threads=threads, scanned_threads=len(threads),
            next_before_id=next_before_id,
            endpoint_exhausted=len(threads) < limit,
            mode=("private_joined" if joined else "private_all")
            if private else "public",
        )


__all__ = [
    "DiscordArchivedThreadPage", "DiscordThreadDiscovery",
    "DiscordThreadDiscoveryError",
]
