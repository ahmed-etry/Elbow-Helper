"""Search Discord messages within checked channels and time bounds."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from ..access import accessible_message_channel
from ..models import AgentRequestContext
from ..capabilities.validation import bounded_int, bounded_text
from .history import (
    SEARCH_RESULT_LIMIT,
    cursor_state,
    search_cursor,
    search_date,
    request_message_boundary,
    searchable_channel_ids,
    jump_url,
)

async def search_discord_messages(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    scope = await _search_scope(context, arguments)
    if "error" in scope:
        return scope
    if not scope["channel_ids"]:
        return {"query": scope["query"], "matches": [], "search_is_exhaustive": False}
    found = await _search_raw(context, arguments, scope)
    if isinstance(found, dict):
        return found
    page, raw_results = found
    matches = await _search_matches(context, arguments, scope, raw_results)
    result = {
        "query": scope["query"], "matches": matches,
        "search_is_exhaustive": False,
        "filters": {key: value for key, value in arguments.items()
                    if key not in {"limit", "cursor"}},
    }
    if page is not None:
        result["coverage"] = _search_coverage(page, matches, scope)
    return result


async def _search_scope(context, arguments):
    query = str(arguments.get("query") or "").strip()
    if not query and not any(arguments.get(key) for key in (
        "channel_id", "channel_ids", "author_id", "after", "before",
    )):
        return {"error": "Supply search words, a channel, an author, or a date range."}
    limit = bounded_int(arguments.get("limit"), default=5, minimum=1,
                        maximum=SEARCH_RESULT_LIMIT)
    requested_channel = arguments.get("channel_id")
    requested_channels = arguments.get("channel_ids")
    if requested_channel is not None and requested_channels is not None:
        return {"error": "Use channel_id or channel_ids, not both."}
    explicit_ids = ((requested_channel,) if requested_channel is not None
                    else tuple(requested_channels or ()))
    if explicit_ids:
        if (len(explicit_ids) > 20 or len(set(explicit_ids)) != len(explicit_ids)
                or any(type(value) is not int or value <= 0 for value in explicit_ids)):
            return {"error": "Supply between one and twenty distinct channel IDs."}
        channels = []
        for channel_id in explicit_ids:
            channel = await accessible_message_channel(context, channel_id)
            if channel is None:
                return {"error": "The asker cannot access every requested conversation."}
            channels.append(channel)
        channel_ids = explicit_ids
    else:
        if arguments.get("cursor") is not None:
            return {"error": (
                "A continuation cursor requires one explicit channel so "
                "the source scope stays stable."
            )}
        channel_ids = searchable_channel_ids(context)
    paged_channel = channel_ids[0] if len(explicit_ids) == 1 else None
    if paged_channel is not None and channels[0] is None:
        return {"error": "The asker cannot access that conversation."}
    try:
        after = search_date(arguments.get("after"))
        before = search_date(arguments.get("before"))
    except ValueError:
        return {"error": "Use ISO 8601 dates or timestamps for the search period."}
    if after is not None and before is not None and after >= before:
        return {"error": "The search start must be earlier than its end."}
    min_id = discord.utils.time_snowflake(after) - 1 if after is not None else None
    requested_max_id = discord.utils.time_snowflake(before) if before is not None else None
    base_scope = {
        "guild_id": context.guild.id, "query": query, "channel_id": paged_channel,
        "author_id": arguments.get("author_id"), "min_id": min_id,
        "requested_max_id": requested_max_id,
    }
    offset, max_id, cursor_scope = 0, requested_max_id, None
    if paged_channel is not None:
        try:
            offset, max_id, cursor_scope = cursor_state(
                arguments.get("cursor"), base_scope,
                requested_max_id or request_message_boundary(context),
            )
        except ValueError:
            return {"error": (
                "That continuation cursor does not match this channel search "
                "and its filters."
            )}
    return {"query": query, "limit": limit, "channel_ids": channel_ids,
            "explicit_ids": explicit_ids, "paged_channel": paged_channel,
            "min_id": min_id, "max_id": max_id, "offset": offset,
            "cursor_scope": cursor_scope}


async def _search_raw(context, arguments, scope):
    channel_ids = scope["channel_ids"]
    paged = scope["paged_channel"]
    if paged is not None:
        page = await context.message_search.search_page(
            guild_id=context.guild.id, content=scope["query"],
            limit=scope["limit"], offset=scope["offset"],
            channel_ids=channel_ids, author_id=arguments.get("author_id"),
            min_id=scope["min_id"], max_id=scope["max_id"],
        )
        if await accessible_message_channel(context, paged) is None:
            return {"error": "The asker cannot access that conversation."}
        if any(
            result.channel_id != paged
            or (arguments.get("author_id") is not None
                and result.author_id != arguments["author_id"])
            or (scope["min_id"] is not None and result.message_id <= scope["min_id"])
            or (scope["max_id"] is not None and result.message_id >= scope["max_id"])
            for result in page.messages
        ):
            return {"error": "Discord returned results outside the requested search scope."}
        context.state.source_channels.add(paged)
        return page, page.messages
    raw = await context.message_search.search(
        guild_id=context.guild.id, content=scope["query"], limit=scope["limit"],
        channel_ids=channel_ids, author_id=arguments.get("author_id"),
        min_id=scope["min_id"], max_id=scope["max_id"],
    )
    if scope["explicit_ids"]:
        for channel_id in scope["explicit_ids"]:
            if await accessible_message_channel(context, channel_id) is None:
                return {"error": "The asker cannot access every requested conversation."}
        if any(result.channel_id not in scope["explicit_ids"] for result in raw):
            return {"error": "Discord returned results outside the requested search scope."}
        context.state.source_channels.update(scope["explicit_ids"])
    return None, raw


async def _search_matches(context, arguments, scope, raw_results):
    matches: list[dict[str, Any]] = []
    for result in raw_results:
        if result.channel_id not in scope["channel_ids"]:
            continue
        if arguments.get("author_id") is not None and result.author_id != arguments["author_id"]:
            continue
        if scope["min_id"] is not None and result.message_id <= scope["min_id"]:
            continue
        if scope["max_id"] is not None and result.message_id >= scope["max_id"]:
            continue
        channel = await accessible_message_channel(context, result.channel_id)
        if channel is None:
            continue
        context.state.source_channels.add(result.channel_id)
        matches.append({
            "message_id": result.message_id, "channel_id": result.channel_id,
            "channel": getattr(channel, "name", str(result.channel_id)),
            "author_id": result.author_id, "author": result.author_name,
            "timestamp": result.timestamp,
            "content": bounded_text(result.content, 1_600),
            "source": jump_url(context.guild.id, result.channel_id, result.message_id),
        })
        if len(matches) >= scope["limit"]:
            break
    return matches


def _search_coverage(page, matches, scope):
    assert scope["cursor_scope"] is not None
    return {
        "offset": page.offset, "requested_page_size": page.limit,
        "returned_indexed_matches": len(page.messages),
        "returned_accessible_matches": len(matches),
        "total_results_estimate": page.total_results,
        "next_offset": page.next_offset,
        "next_cursor": (search_cursor(page.next_offset, scope["cursor_scope"])
                        if page.next_offset is not None else None),
        "snapshot_before_message_id": scope["max_id"],
        "deep_historical_indexing": page.deep_historical_indexing,
        "offset_limit_reached": page.offset_limit_reached,
        "reached_current_indexed_end": (
            page.next_offset is None and not page.deep_historical_indexing
            and not page.offset_limit_reached
        ),
        "total_may_change_while_messages_are_created_or_deleted": True,
    }


