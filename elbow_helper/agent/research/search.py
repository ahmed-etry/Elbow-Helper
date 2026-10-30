"""Search Discord messages within checked channels and time bounds."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import json
from typing import Any

import discord

from ..access import accessible_message_channel, message_channel_unavailable
from ..models import AgentRequestContext
from ..capabilities.validation import bounded_int, bounded_text
from .history import (
    SEARCH_RESULT_LIMIT,
    COMPLETE_HISTORY_CHARACTER_BUDGET,
    HISTORY_MESSAGE_CHARACTER_BUDGET,
    cursor_state,
    search_cursor,
    search_date,
    request_message_boundary,
    searchable_channel_ids,
    jump_url,
    search_message_text,
)

async def search_discord_messages(
    context: AgentRequestContext,
    arguments: Mapping[str, Any], *, complete_content: bool = False,
) -> Mapping[str, Any]:
    pages = arguments.get("pages", 1)
    if type(pages) is not int or not 1 <= pages <= 8:
        return {"error": "Choose between one and eight history pages."}
    scope = await _search_scope(context, arguments)
    if "error" in scope:
        return scope
    if not scope["accessible_ids"]:
        return {"query": scope["query"], "matches": [], "search_is_exhaustive": False}
    remaining = (COMPLETE_HISTORY_CHARACTER_BUDGET if complete_content
                 else HISTORY_MESSAGE_CHARACTER_BUDGET)
    matches = []
    coverage = None
    used = 0
    for _ in range(pages):
        reserve = getattr(context, "reserve_search_page", None)
        if reserve is not None:
            available = await reserve(used)
            if available <= 0:
                if coverage is None:
                    return {"error": "The request reached its evidence limit."}
                coverage["stopped_at_request_budget"] = True
                break
            remaining = min(remaining, available)
        found = await _search_raw(context, arguments, scope)
        if isinstance(found, dict):
            # Access loss discards earlier pages as well.
            return found
        page, raw_results = found
        batch, scanned, characters, stopped = await _search_matches(
            context, arguments, scope, raw_results, character_budget=remaining,
            complete_content=complete_content,
        )
        if stopped:
            page = replace(page, next_offset=page.offset + scanned)
        current = _search_coverage(page, batch, scope)
        if "returned_indexed_matches" in current:
            current["returned_indexed_matches"] = scanned
        current.update(stopped_at_message_budget=stopped, pages_scanned=1,
                       order="newest_first")
        if coverage is None:
            coverage = current
        else:
            for field in ("returned_accessible_matches", "returned_indexed_matches",
                          "out_of_scope_results_dropped", "pages_scanned"):
                if field in current:
                    coverage[field] = coverage.get(field, 0) + current[field]
            for field in ("next_offset", "next_cursor", "deep_historical_indexing",
                          "offset_limit_reached", "reached_current_indexed_end",
                          "stopped_at_message_budget"):
                coverage[field] = current[field]
        matches.extend(batch)
        remaining -= characters
        used += characters
        if page.next_offset is None or remaining <= 0 or stopped:
            break
        scope["offset"] = page.next_offset
    if scope["missing_sources"] or coverage.get("out_of_scope_results_dropped"):
        coverage.pop("total_results_estimate", None)
    if scope["missing_sources"]:
        coverage.pop("returned_indexed_matches", None)
    result = {
        "query": scope["query"], "matches": matches,
        "search_is_exhaustive": False,
        "filters": {key: value for key, value in arguments.items()
                    if key not in {"limit", "cursor", "pages"}},
    }
    result["coverage"] = coverage
    if scope["missing_sources"]:
        result["missing_sources"] = scope["missing_sources"]
    return result


async def _search_scope(context, arguments):
    query = str(arguments.get("query") or "").strip()
    if not query and not any(arguments.get(key) for key in (
        "channel_id", "channel_ids", "author_id", "author_ids", "mentions", "after", "before",
    )):
        return {"error": "Supply search words, a channel, an author, or a date range."}
    limit = bounded_int(arguments.get("limit"), default=25, minimum=1,
                        maximum=SEARCH_RESULT_LIMIT)
    requested_channel = arguments.get("channel_id")
    requested_channels = arguments.get("channel_ids")
    if requested_channel is not None and requested_channels is not None:
        return {"error": "Use channel_id or channel_ids, not both."}
    if requested_channels is not None and not isinstance(requested_channels, (list, tuple)):
        return {"error": "Supply between one and twenty distinct channel IDs."}
    explicit_ids = ((requested_channel,) if requested_channel is not None
                    else tuple(requested_channels or ()))
    if requested_channels is not None and not explicit_ids:
        return {"error": "Supply between one and twenty distinct channel IDs."}
    missing_sources = []
    if explicit_ids:
        if (len(explicit_ids) > 20
                or any(type(value) is not int or value <= 0 for value in explicit_ids)
                or len(set(explicit_ids)) != len(explicit_ids)):
            return {"error": "Supply between one and twenty distinct channel IDs."}
        accessible_ids = []
        for channel_id in explicit_ids:
            if await accessible_message_channel(context, channel_id) is None:
                missing_sources.append({"channel_id": channel_id, "cause": "source_unavailable"})
            else:
                accessible_ids.append(channel_id)
        if not accessible_ids:
            return message_channel_unavailable(context, explicit_ids[0])
        channel_ids = tuple(accessible_ids)
    else:
        # Discord searches the guild itself. The local directory is only a
        # disclosure allowlist, never an API channel filter.
        channel_ids = ()
        accessible_ids = await searchable_channel_ids(context, limit=None)
    try:
        after = search_date(arguments.get("after"))
        before = search_date(arguments.get("before"))
    except (ValueError, TypeError, AttributeError):
        return {"error": "Use ISO 8601 dates or timestamps for the search period."}
    if after is not None and before is not None and after >= before:
        return {"error": "The search start must be earlier than its end."}
    min_id = discord.utils.time_snowflake(after) - 1 if after is not None else None
    requested_max_id = discord.utils.time_snowflake(before) if before is not None else None
    author_ids = arguments.get("author_ids", ())
    author_id = arguments.get("author_id")
    mentions = arguments.get("mentions", ())
    if (not isinstance(author_ids, (list, tuple)) or len(author_ids) > 200
            or any(type(value) is not int or value <= 0 for value in author_ids)
            or (author_id is not None and (type(author_id) is not int or author_id <= 0))):
        return {"error": "Supply valid author member IDs."}
    author_ids = tuple(sorted(set(author_ids) | ({author_id} if author_id is not None else set())))
    if (not isinstance(mentions, (list, tuple)) or len(mentions) > 200
            or any(type(value) is not int or value <= 0 for value in mentions)):
        return {"error": "Supply valid mentioned member IDs."}
    base_scope = {
        "guild_id": context.guild.id, "requester_id": context.member.id,
        "query": query, "channel_ids": sorted(explicit_ids),
        "author_ids": author_ids, "mentions": sorted(set(mentions)), "min_id": min_id,
        "requested_max_id": requested_max_id,
    }
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
            "accessible_ids": set(accessible_ids), "explicit_ids": explicit_ids,
            "author_ids": author_ids, "missing_sources": missing_sources,
            "min_id": min_id, "max_id": max_id, "offset": offset,
            "cursor_scope": cursor_scope}


async def _search_raw(context, arguments, scope):
    scope["out_of_scope_results_dropped"] = 0
    filters = {}
    if scope["channel_ids"]:
        filters["channel_ids"] = scope["channel_ids"]
    if arguments.get("author_ids"):
        filters["author_ids"] = scope["author_ids"]
    elif arguments.get("author_id") is not None:
        filters["author_id"] = arguments["author_id"]
    if arguments.get("mentions"):
        filters["mentions"] = arguments["mentions"]
    page = await context.message_search.search_page(
        guild_id=context.guild.id, content=scope["query"],
        limit=scope["limit"], offset=scope["offset"],
        min_id=scope["min_id"], max_id=scope["max_id"], **filters,
    )
    if scope["explicit_ids"]:
        for channel_id in scope["channel_ids"]:
            if await accessible_message_channel(context, channel_id) is None:
                scope["accessible_ids"].discard(channel_id)
                scope["missing_sources"].append({"channel_id": channel_id, "cause": "source_unavailable"})
        if not any(channel_id in scope["accessible_ids"] for channel_id in scope["channel_ids"]):
            return message_channel_unavailable(context, scope["explicit_ids"][0])
        checked_channels = {}
        for result in page.messages:
            in_channel_scope = result.channel_id in scope["channel_ids"]
            if not in_channel_scope:
                if result.channel_id not in checked_channels:
                    channel = context.guild.get_channel_or_thread(result.channel_id)
                    if channel is None:
                        try:
                            channel = await context.bot.fetch_channel(result.channel_id)
                        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                            channel = None
                    checked_channels[result.channel_id] = (
                        isinstance(channel, discord.Thread)
                        and channel.parent_id in scope["channel_ids"]
                        and channel.parent_id in scope["accessible_ids"]
                    )
                in_channel_scope = checked_channels[result.channel_id]
                if in_channel_scope:
                    scope["accessible_ids"].add(result.channel_id)
                else:
                    scope["accessible_ids"].discard(result.channel_id)
            if (not in_channel_scope
                    or (scope["author_ids"] and result.author_id not in scope["author_ids"])
                    or (scope["min_id"] is not None and result.message_id <= scope["min_id"])
                    or (scope["max_id"] is not None and result.message_id >= scope["max_id"])):
                scope["out_of_scope_results_dropped"] += 1
        context.state.source_channels.update(set(scope["channel_ids"]) & scope["accessible_ids"])
    return page, page.messages


async def _search_matches(context, arguments, scope, raw_results, *, character_budget,
                          complete_content=False):
    matches: list[dict[str, Any]] = []
    characters = 0
    scanned = 0
    stopped = False
    for index, result in enumerate(raw_results):
        scanned = index + 1
        if result.channel_id not in scope["accessible_ids"]:
            continue
        if scope["author_ids"] and result.author_id not in scope["author_ids"]:
            continue
        if scope["min_id"] is not None and result.message_id <= scope["min_id"]:
            continue
        if scope["max_id"] is not None and result.message_id >= scope["max_id"]:
            continue
        channel = await accessible_message_channel(context, result.channel_id)
        if channel is None:
            continue
        context.state.source_channels.add(result.channel_id)
        entry = {
            "message_id": result.message_id, "channel_id": result.channel_id,
            "channel": getattr(channel, "name", str(result.channel_id)),
            "author_id": result.author_id, "author": result.author_name,
            "timestamp": result.timestamp,
            "content": result.content if complete_content else bounded_text(search_message_text(result, context.guild), 1_600),
            "source": jump_url(context.guild.id, result.channel_id, result.message_id),
        }
        if complete_content:
            entry.update(embeds=list(result.embeds), attachment_names=list(result.attachment_names), content_complete=True)
        if result.reply_to_message_id is not None:
            entry["reply_to_message_id"] = result.reply_to_message_id
        size = len(json.dumps(entry, ensure_ascii=False, separators=(",", ":"))) + 1
        if characters + size > character_budget:
            scanned = index
            stopped = True
            break
        matches.append(entry)
        characters += size
        if len(matches) >= scope["limit"]:
            break
    return matches, scanned, characters, stopped


def _search_coverage(page, matches, scope):
    assert scope["cursor_scope"] is not None
    coverage = {
        "offset": page.offset, "requested_page_size": page.limit,
        "returned_accessible_matches": len(matches),
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
    if scope["explicit_ids"] and not scope["missing_sources"]:
        coverage["total_results_estimate"] = page.total_results
        coverage["returned_indexed_matches"] = len(page.messages)
    if scope["out_of_scope_results_dropped"]:
        coverage["out_of_scope_results_dropped"] = scope["out_of_scope_results_dropped"]
    return coverage
