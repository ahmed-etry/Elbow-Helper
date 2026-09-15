"""Bounded access to Discord's guild-message search endpoint."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from discord.http import Route


SEARCH_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 2.0


class DiscordMessageSearchError(RuntimeError):
    """Raised when Discord does not return a usable message-search response."""


@dataclass(frozen=True, slots=True)
class DiscordSearchMessage:
    """Provider-independent subset of a Discord message search result."""

    message_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    timestamp: str


class DiscordMessageSearch:
    """Search guild messages through the bot-owned Discord HTTP transport."""

    def __init__(self, http: Any):
        self._http = http

    async def search(
        self,
        *,
        guild_id: int,
        content: str,
        limit: int,
        channel_ids: Sequence[int] = (),
    ) -> tuple[DiscordSearchMessage, ...]:
        query = str(content or "").strip()
        if not query:
            return ()

        bounded_limit = max(1, min(25, int(limit)))
        route = Route(
            "GET",
            "/guilds/{guild_id}/messages/search",
            guild_id=guild_id,
        )
        payload: Any = None
        for attempt in range(SEARCH_ATTEMPTS):
            params: list[tuple[str, str | int]] = [
                ("content", query[:1024]),
                ("limit", bounded_limit),
            ]
            params.extend(
                ("channel_id", str(channel_id))
                for channel_id in channel_ids[:500]
                if int(channel_id) > 0
            )
            payload = await self._http.request(route, params=params)
            if not _index_pending(payload):
                break
            if attempt + 1 >= SEARCH_ATTEMPTS:
                raise DiscordMessageSearchError(
                    "Discord message search is still indexing"
                )
            retry_after = _retry_after(payload)
            await asyncio.sleep(retry_after)

        if not isinstance(payload, dict):
            raise DiscordMessageSearchError(
                "Discord returned an invalid message-search response"
            )
        raw_groups = payload.get("messages")
        if not isinstance(raw_groups, list):
            raise DiscordMessageSearchError(
                "Discord message search omitted its results"
            )

        results: list[DiscordSearchMessage] = []
        seen_ids: set[int] = set()
        for group in raw_groups:
            candidates = group if isinstance(group, list) else [group]
            for raw in candidates:
                result = _parse_search_message(raw)
                if result is None or result.message_id in seen_ids:
                    continue
                seen_ids.add(result.message_id)
                results.append(result)
                if len(results) >= bounded_limit:
                    return tuple(results)
        return tuple(results)


def _index_pending(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    try:
        return int(payload.get("code") or 0) == 110000
    except (TypeError, ValueError):
        return False


def _retry_after(payload: dict[str, Any]) -> float:
    try:
        seconds = float(payload.get("retry_after") or 0.1)
    except (TypeError, ValueError):
        seconds = 0.1
    if not math.isfinite(seconds) or seconds > MAX_RETRY_DELAY_SECONDS:
        raise DiscordMessageSearchError("Discord message search needs more time to index")
    return max(0.1, seconds)


def _parse_search_message(raw: object) -> DiscordSearchMessage | None:
    if not isinstance(raw, dict):
        return None
    author = raw.get("author")
    if not isinstance(author, dict):
        author = {}
    try:
        message_id = int(raw.get("id") or 0)
        channel_id = int(raw.get("channel_id") or 0)
        author_id = int(author.get("id") or 0)
    except (TypeError, ValueError):
        return None
    if message_id <= 0 or channel_id <= 0:
        return None
    author_name = str(
        author.get("global_name")
        or author.get("username")
        or author_id
        or "Unknown"
    )
    return DiscordSearchMessage(
        message_id=message_id,
        channel_id=channel_id,
        author_id=author_id,
        author_name=author_name,
        content=str(raw.get("content") or ""),
        timestamp=str(raw.get("timestamp") or ""),
    )
