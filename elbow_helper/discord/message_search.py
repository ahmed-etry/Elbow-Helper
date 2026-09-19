"""Bounded access to Discord's guild-message search endpoint."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from discord.http import Route


SEARCH_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 2.0
MAX_SEARCH_OFFSET = 9_975
MAX_SEARCH_QUERY_STRING_BYTES = 3_000


class DiscordMessageSearchError(RuntimeError):
    """Raised when Discord does not return a usable message-search response."""


class DiscordMessageHistoryError(RuntimeError):
    """Raised when Discord does not return a usable channel-history page."""


@dataclass(frozen=True, slots=True)
class DiscordSearchMessage:
    """Provider-independent subset of a Discord message search result."""

    message_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    timestamp: str


@dataclass(frozen=True, slots=True)
class DiscordSearchPage:
    """One explicit API page plus honest continuation/indexing metadata."""

    messages: tuple[DiscordSearchMessage, ...]
    offset: int
    limit: int
    total_results: int
    next_offset: int | None
    deep_historical_indexing: bool
    offset_limit_reached: bool


@dataclass(frozen=True, slots=True)
class DiscordHistoryPage:
    """One newest-first channel-history page within fixed snowflake bounds."""

    messages: tuple[DiscordSearchMessage, ...]
    before_id: int
    after_id: int
    limit: int
    next_before_id: int | None
    reached_window_start: bool


class DiscordMessageSearch:
    """Read and search messages through the bot-owned Discord HTTP transport."""

    def __init__(self, http: Any):
        self._http = http

    async def search(
        self,
        *,
        guild_id: int,
        content: str,
        limit: int,
        channel_ids: Sequence[int] = (),
        author_id: int | None = None,
        min_id: int | None = None,
        max_id: int | None = None,
    ) -> tuple[DiscordSearchMessage, ...]:
        bounded_limit = max(1, min(25, int(limit)))
        batches = _search_channel_batches(
            content=content, limit=bounded_limit, offset=0,
            channel_ids=channel_ids, author_id=author_id,
            min_id=min_id, max_id=max_id,
        )
        results: dict[int, DiscordSearchMessage] = {}
        for batch in batches:
            page = await self.search_page(
                guild_id=guild_id, content=content, limit=bounded_limit,
                channel_ids=batch, author_id=author_id,
                min_id=min_id, max_id=max_id,
            )
            for message in page.messages:
                results[message.message_id] = message
        return tuple(sorted(
            results.values(),
            key=lambda message: (message.timestamp, message.message_id),
            reverse=True,
        )[:bounded_limit])

    async def search_page(
        self,
        *,
        guild_id: int,
        content: str,
        limit: int,
        offset: int = 0,
        channel_ids: Sequence[int] = (),
        author_id: int | None = None,
        min_id: int | None = None,
        max_id: int | None = None,
    ) -> DiscordSearchPage:
        query = str(content or "").strip()
        if not query and not (channel_ids or author_id or min_id or max_id):
            return DiscordSearchPage((), 0, 0, 0, None, False, False)

        if type(offset) is not int or not 0 <= offset <= MAX_SEARCH_OFFSET:
            raise ValueError("Invalid Discord search offset")

        bounded_limit = max(1, min(25, int(limit)))
        route = Route(
            "GET",
            "/guilds/{guild_id}/messages/search",
            guild_id=guild_id,
        )
        payload: Any = None
        for attempt in range(SEARCH_ATTEMPTS):
            params = _search_params(
                content=query, limit=bounded_limit, offset=offset,
                channel_ids=channel_ids, author_id=author_id,
                min_id=min_id, max_id=max_id,
            )
            if len(urlencode(params).encode("ascii")) > MAX_SEARCH_QUERY_STRING_BYTES:
                raise DiscordMessageSearchError(
                    "Discord message search parameters are too large"
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

        total_results = payload.get("total_results")
        deep_indexing = payload.get("doing_deep_historical_index", False)
        if (
            type(total_results) is not int
            or total_results < 0
            or type(deep_indexing) is not bool
        ):
            raise DiscordMessageSearchError(
                "Discord message search omitted its coverage metadata"
            )

        results: list[DiscordSearchMessage] = []
        seen_ids: set[int] = set()
        for group in raw_groups:
            candidates = group if isinstance(group, list) else [group]
            parsed_group = 0
            for raw in candidates:
                result = _parse_search_message(raw)
                if result is None:
                    continue
                if result.message_id in seen_ids:
                    raise DiscordMessageSearchError(
                        "Discord message search repeated a result identity"
                    )
                seen_ids.add(result.message_id)
                results.append(result)
                parsed_group += 1
                if len(results) >= bounded_limit:
                    break
            if parsed_group == 0:
                raise DiscordMessageSearchError(
                    "Discord message search returned an invalid result group"
                )
            if len(results) >= bounded_limit:
                break
        next_candidate = offset + bounded_limit
        offset_limited = (
            next_candidate < total_results
            and next_candidate > MAX_SEARCH_OFFSET
        )
        next_offset = (
            next_candidate
            if next_candidate < total_results
            and next_candidate <= MAX_SEARCH_OFFSET
            else None
        )
        return DiscordSearchPage(
            messages=tuple(results),
            offset=offset,
            limit=bounded_limit,
            total_results=total_results,
            next_offset=next_offset,
            deep_historical_indexing=deep_indexing,
            offset_limit_reached=offset_limited,
        )

    async def history_page(
        self,
        *,
        channel_id: int,
        before_id: int,
        after_id: int,
        limit: int,
    ) -> DiscordHistoryPage:
        """Read one stable newest-first page, enforcing an exclusive ID window."""

        if any(
            type(value) is not int or value <= 0
            for value in (channel_id, before_id, after_id)
        ) or after_id >= before_id:
            raise ValueError("Invalid Discord history scope")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid Discord history page size")

        route = Route(
            "GET", "/channels/{channel_id}/messages", channel_id=channel_id,
        )
        payload = await self._http.request(
            route,
            params=[("limit", limit), ("before", str(before_id))],
        )
        if not isinstance(payload, list) or len(payload) > limit:
            raise DiscordMessageHistoryError(
                "Discord returned an invalid channel-history response"
            )

        parsed: list[DiscordSearchMessage] = []
        previous_id = before_id
        reached_window_start = len(payload) < limit
        for raw in payload:
            message = _parse_search_message(raw)
            if (
                message is None
                or message.channel_id != channel_id
                or message.message_id >= previous_id
            ):
                raise DiscordMessageHistoryError(
                    "Discord returned an invalid channel-history page"
                )
            previous_id = message.message_id
            if message.message_id <= after_id:
                reached_window_start = True
                continue
            parsed.append(message)

        next_before_id = None
        if not reached_window_start and parsed:
            next_before_id = parsed[-1].message_id
            if next_before_id >= before_id:
                raise DiscordMessageHistoryError(
                    "Discord channel-history cursor did not advance"
                )
        elif not reached_window_start:
            raise DiscordMessageHistoryError(
                "Discord channel-history page did not advance"
            )
        return DiscordHistoryPage(
            messages=tuple(parsed), before_id=before_id, after_id=after_id,
            limit=limit, next_before_id=next_before_id,
            reached_window_start=reached_window_start,
        )


def _search_params(
    *, content: str, limit: int, offset: int,
    channel_ids: Sequence[int], author_id: int | None,
    min_id: int | None, max_id: int | None,
) -> list[tuple[str, str | int]]:
    params: list[tuple[str, str | int]] = [("limit", limit), ("offset", offset)]
    query = str(content or "").strip()
    if query:
        params.insert(0, ("content", query[:1024]))
    for name, value in (
        ("author_id", author_id), ("min_id", min_id), ("max_id", max_id),
    ):
        if value is not None:
            params.append((name, str(value)))
    params.extend(
        ("channel_id", str(channel_id))
        for channel_id in channel_ids
        if type(channel_id) is int and channel_id > 0
    )
    return params


def _search_channel_batches(
    *, content: str, limit: int, offset: int,
    channel_ids: Sequence[int], author_id: int | None,
    min_id: int | None, max_id: int | None,
) -> tuple[tuple[int, ...], ...]:
    valid_ids = tuple(dict.fromkeys(
        channel_id for channel_id in channel_ids
        if type(channel_id) is int and channel_id > 0
    ))
    if not valid_ids:
        params = _search_params(
            content=content, limit=limit, offset=offset, channel_ids=(),
            author_id=author_id, min_id=min_id, max_id=max_id,
        )
        if len(urlencode(params).encode("ascii")) > MAX_SEARCH_QUERY_STRING_BYTES:
            raise DiscordMessageSearchError(
                "Discord message search parameters are too large"
            )
        return ((),)

    batches: list[tuple[int, ...]] = []
    current: list[int] = []
    for channel_id in valid_ids:
        candidate = (*current, channel_id)
        params = _search_params(
            content=content, limit=limit, offset=offset,
            channel_ids=candidate, author_id=author_id,
            min_id=min_id, max_id=max_id,
        )
        if len(urlencode(params).encode("ascii")) <= MAX_SEARCH_QUERY_STRING_BYTES:
            current.append(channel_id)
            continue
        if not current:
            raise DiscordMessageSearchError(
                "Discord message search parameters are too large"
            )
        batches.append(tuple(current))
        current = [channel_id]
    if current:
        batches.append(tuple(current))
    return tuple(batches)


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
    content = raw.get("content")
    timestamp = raw.get("timestamp")
    if (
        message_id <= 0
        or channel_id <= 0
        or author_id <= 0
        or not isinstance(content, str)
        or not isinstance(timestamp, str)
        or not timestamp
        or len(timestamp) > 64
    ):
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
        content=content,
        timestamp=timestamp,
    )
