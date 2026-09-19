"""Discord history tools with asker-specific permission enforcement."""

from __future__ import annotations

from typing import Any
from typing import Mapping
from datetime import datetime, timezone
import hashlib
import json

import discord

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import accessible_message_channel
from ..models import AgentRequestContext
from ..message_content import message_text
from ..models import RegisteredAgentTool
from .shared import bounded_int
from .shared import bounded_text
from .shared import positive_int


SEARCH_RESULT_LIMIT = 25
CONTEXT_MESSAGE_LIMIT = 50
HISTORY_PAGE_LIMIT = 25


def discord_tools() -> tuple[RegisteredAgentTool, ...]:
    return (
        RegisteredAgentTool(
            AgentToolDefinition(
                name="find_discord_channels",
                description="Find accessible Discord channels by name or ID. Use the result to identify the channel requested by the asker.",
                parameters={
                    "type": "object", "properties": {"query": {"type": "string", "maxLength": 100}},
                    "required": ["query"], "additionalProperties": False,
                },
            ),
            find_discord_channels,
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="search_discord_messages",
                description=(
                    "Search accessible Discord history by words or phrases, optionally restricted "
                    "to one or more channels, an author, or a date range. Honour the scope requested by the asker. "
                    "For one explicit channel, continue with the returned cursor when broader coverage "
                    "is needed. Results are not proof of absence; read surrounding messages when needed."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Narrow words or phrase to search for.",
                            "minLength": 1,
                            "maxLength": 1024,
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": SEARCH_RESULT_LIMIT,
                            "default": 5,
                        },
                        "channel_id": {"type": "integer", "minimum": 1},
                        "channel_ids": {
                            "type": "array", "minItems": 1, "maxItems": 20,
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                            "description": "Explicit accessible channels to search together.",
                        },
                        "cursor": {
                            "type": "string", "maxLength": 64,
                            "description": "Continuation cursor returned by the same channel search and filters.",
                        },
                        "author_id": {"type": "integer", "minimum": 1},
                        "after": {"type": "string", "maxLength": 40, "description": "Inclusive ISO 8601 date or timestamp. Use UTC when no offset is supplied."},
                        "before": {"type": "string", "maxLength": 40, "description": "Exclusive ISO 8601 date or timestamp. Use UTC when no offset is supplied."},
                    },
                    "additionalProperties": False,
                },
            ),
            search_discord_messages,
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="read_discord_channel_history",
                description=(
                    "Read one bounded page of every currently available message "
                    "in one accessible Discord channel or thread from an inclusive "
                    "start through an exclusive end (or the current request). This "
                    "does not depend on keyword search indexing; continue with the "
                    "returned cursor for broader coverage."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "channel_id": {"type": "integer", "minimum": 1},
                        "after": {
                            "type": "string", "maxLength": 40,
                            "description": (
                                "Inclusive ISO 8601 start date or timestamp. "
                                "Use UTC when no offset is supplied."
                            ),
                        },
                        "before": {
                            "type": "string", "maxLength": 40,
                            "description": (
                                "Exclusive ISO 8601 end date or timestamp. "
                                "Omit to stop at the current request."
                            ),
                        },
                        "author_id": {"type": "integer", "minimum": 1},
                        "cursor": {
                            "type": "string", "maxLength": 64,
                            "description": (
                                "Continuation cursor returned for this same "
                                "channel, period and author filter."
                            ),
                        },
                        "limit": {
                            "type": "integer", "minimum": 1,
                            "maximum": HISTORY_PAGE_LIMIT, "default": 25,
                        },
                    },
                    "required": ["channel_id", "after"],
                    "additionalProperties": False,
                },
            ),
            read_discord_channel_history,
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="read_message_context",
                description=(
                    "Read messages surrounding an accessible Discord message identified by a search "
                    "result or message link, so its meaning can be assessed in context."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "channel_id": {"type": "integer", "minimum": 1},
                        "message_id": {"type": "integer", "minimum": 1},
                        "limit": {
                            "type": "integer",
                            "minimum": 3,
                            "maximum": CONTEXT_MESSAGE_LIMIT,
                            "default": 15,
                        },
                    },
                    "required": ["channel_id", "message_id"],
                    "additionalProperties": False,
                },
            ),
            read_message_context,
        ),
    )


async def read_discord_channel_history(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    channel_id = positive_int(arguments.get("channel_id"))
    author_id = arguments.get("author_id")
    if channel_id is None or (
        author_id is not None and positive_int(author_id) is None
    ):
        return {"error": "Valid channel and author IDs are required."}
    channel = await accessible_message_channel(context, channel_id)
    if channel is None:
        return {"error": "The asker cannot access that conversation."}
    try:
        after = _search_date(arguments.get("after"))
        before = _search_date(arguments.get("before"))
        if after is None:
            raise ValueError("Missing history start")
        effective_before = before or _request_time(context)
        if after >= effective_before:
            raise ValueError("Invalid history period")
    except ValueError:
        return {
            "error": (
                "Use a valid ISO 8601 start before the requested history end."
            )
        }
    after_id = discord.utils.time_snowflake(after) - 1
    requested_before_id = (
        discord.utils.time_snowflake(before) if before is not None else None
    )
    if after_id <= 0:
        return {"error": "The requested history start is outside Discord history."}
    base_scope = {
        "guild_id": context.guild.id,
        "channel_id": channel_id,
        "author_id": author_id,
        "after_id": after_id,
        "requested_before_id": requested_before_id,
    }
    try:
        page_before_id, cursor_scope = _history_cursor_state(
            arguments.get("cursor"), base_scope,
            requested_before_id or _request_message_boundary(context),
        )
    except ValueError:
        return {
            "error": (
                "That continuation cursor does not match this channel history "
                "period and its filters."
            )
        }
    limit = bounded_int(
        arguments.get("limit"), default=HISTORY_PAGE_LIMIT,
        minimum=1, maximum=HISTORY_PAGE_LIMIT,
    )
    page = await context.message_search.history_page(
        channel_id=channel_id, before_id=page_before_id,
        after_id=after_id, limit=limit,
    )
    if await accessible_message_channel(context, channel_id) is None:
        return {"error": "The asker cannot access that conversation."}
    if (
        page.before_id != page_before_id
        or page.after_id != after_id
        or page.limit != limit
        or page.reached_window_start != (page.next_before_id is None)
        or (
            page.next_before_id is not None
            and not after_id < page.next_before_id < page_before_id
        )
        or any(
            message.channel_id != channel_id
            or not after_id < message.message_id < page_before_id
            for message in page.messages
        )
    ):
        return {
            "error": "Discord returned messages outside the requested history scope."
        }
    messages = [
        {
            "message_id": message.message_id,
            "channel_id": channel_id,
            "channel": getattr(channel, "name", str(channel_id)),
            "author_id": message.author_id,
            "author": message.author_name,
            "timestamp": message.timestamp,
            "content": bounded_text(message.content, 1_600),
            "source": jump_url(context.guild.id, channel_id, message.message_id),
        }
        for message in page.messages
        if author_id is None or message.author_id == author_id
    ]
    context.state.source_channels.add(channel_id)
    return {
        "channel_id": channel_id,
        "channel": getattr(channel, "name", str(channel_id)),
        "messages": messages,
        "coverage": {
            "requested_after": after.isoformat(),
            "requested_before": before.isoformat() if before is not None else None,
            "window_after_message_id": after_id,
            "snapshot_before_message_id": cursor_scope["effective_before_id"],
            "page_before_message_id": page.before_id,
            "scanned_messages_in_window": len(page.messages),
            "returned_messages": len(messages),
            "next_cursor": (
                _history_cursor(page.next_before_id, cursor_scope)
                if page.next_before_id is not None else None
            ),
            "reached_requested_start": page.reached_window_start,
            "covers_currently_available_messages_only": True,
        },
    }


async def search_discord_messages(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query and not any(arguments.get(key) for key in ("channel_id", "channel_ids", "author_id", "after", "before")):
        return {"error": "Supply search words, a channel, an author, or a date range."}
    limit = bounded_int(
        arguments.get("limit"),
        default=5,
        minimum=1,
        maximum=SEARCH_RESULT_LIMIT,
    )
    requested_channel = arguments.get("channel_id")
    requested_channels = arguments.get("channel_ids")
    if requested_channel is not None and requested_channels is not None:
        return {"error": "Use channel_id or channel_ids, not both."}
    explicit_channel_ids = (
        (requested_channel,) if requested_channel is not None
        else tuple(requested_channels or ())
    )
    if explicit_channel_ids:
        if (
            len(explicit_channel_ids) > 20
            or len(set(explicit_channel_ids)) != len(explicit_channel_ids)
            or any(type(value) is not int or value <= 0 for value in explicit_channel_ids)
        ):
            return {"error": "Supply between one and twenty distinct channel IDs."}
        channels = []
        for channel_id in explicit_channel_ids:
            channel = await accessible_message_channel(context, channel_id)
            if channel is None:
                return {"error": "The asker cannot access every requested conversation."}
            channels.append(channel)
        channel_ids = explicit_channel_ids
    else:
        if arguments.get("cursor") is not None:
            return {
                "error": (
                    "A continuation cursor requires one explicit channel so "
                    "the source scope stays stable."
                )
            }
        channel_ids = searchable_channel_ids(context)
    paged_channel = channel_ids[0] if len(explicit_channel_ids) == 1 else None
    if paged_channel is not None:
        channel = channels[0]
        if channel is None:
            return {"error": "The asker cannot access that conversation."}
    try:
        after = _search_date(arguments.get("after"))
        before = _search_date(arguments.get("before"))
    except ValueError:
        return {"error": "Use ISO 8601 dates or timestamps for the search period."}
    if after is not None and before is not None and after >= before:
        return {"error": "The search start must be earlier than its end."}
    min_id = discord.utils.time_snowflake(after) - 1 if after is not None else None
    requested_max_id = discord.utils.time_snowflake(before) if before is not None else None
    cursor_base_scope = {
        "guild_id": context.guild.id,
        "query": query,
        "channel_id": paged_channel,
        "author_id": arguments.get("author_id"),
        "min_id": min_id,
        "requested_max_id": requested_max_id,
    }
    offset = 0
    max_id = requested_max_id
    cursor_scope = None
    if paged_channel is not None:
        try:
            offset, max_id, cursor_scope = _cursor_state(
                arguments.get("cursor"),
                cursor_base_scope,
                requested_max_id or _request_message_boundary(context),
            )
        except ValueError:
            return {
                "error": (
                    "That continuation cursor does not match this channel search "
                    "and its filters."
                )
            }
    if not channel_ids:
        return {"query": query, "matches": [], "search_is_exhaustive": False}
    page = None
    if paged_channel is not None:
        page = await context.message_search.search_page(
            guild_id=context.guild.id,
            content=query,
            limit=limit,
            offset=offset,
            channel_ids=channel_ids,
            author_id=arguments.get("author_id"),
            min_id=min_id,
            max_id=max_id,
        )
        if await accessible_message_channel(context, paged_channel) is None:
            return {"error": "The asker cannot access that conversation."}
        if any(
            result.channel_id != paged_channel
            or (
                arguments.get("author_id") is not None
                and result.author_id != arguments["author_id"]
            )
            or (min_id is not None and result.message_id <= min_id)
            or (max_id is not None and result.message_id >= max_id)
            for result in page.messages
        ):
            return {
                "error": (
                    "Discord returned results outside the requested search scope."
                )
            }
        context.state.source_channels.add(paged_channel)
        raw_results = page.messages
    else:
        raw_results = await context.message_search.search(
            guild_id=context.guild.id,
            content=query,
            limit=limit,
            channel_ids=channel_ids,
            author_id=arguments.get("author_id"),
            min_id=min_id,
            max_id=max_id,
        )
        if explicit_channel_ids:
            for channel_id in explicit_channel_ids:
                if await accessible_message_channel(context, channel_id) is None:
                    return {"error": "The asker cannot access every requested conversation."}
            if any(result.channel_id not in explicit_channel_ids for result in raw_results):
                return {"error": "Discord returned results outside the requested search scope."}
            context.state.source_channels.update(explicit_channel_ids)
    matches: list[dict[str, Any]] = []
    for result in raw_results:
        if result.channel_id not in channel_ids:
            continue
        if arguments.get("author_id") is not None and result.author_id != arguments["author_id"]:
            continue
        if min_id is not None and result.message_id <= min_id:
            continue
        if max_id is not None and result.message_id >= max_id:
            continue
        channel = await accessible_message_channel(context, result.channel_id)
        if channel is None:
            continue
        context.state.source_channels.add(result.channel_id)
        matches.append(
            {
                "message_id": result.message_id,
                "channel_id": result.channel_id,
                "channel": getattr(channel, "name", str(result.channel_id)),
                "author_id": result.author_id,
                "author": result.author_name,
                "timestamp": result.timestamp,
                "content": bounded_text(result.content, 1_600),
                "source": jump_url(
                    context.guild.id,
                    result.channel_id,
                    result.message_id,
                ),
            }
        )
        if len(matches) >= limit:
            break
    result = {
        "query": query,
        "matches": matches,
        "search_is_exhaustive": False,
        "filters": {
            key: value for key, value in arguments.items()
            if key not in {"limit", "cursor"}
        },
    }
    if page is not None:
        assert cursor_scope is not None
        result["coverage"] = {
            "offset": page.offset,
            "requested_page_size": page.limit,
            "returned_indexed_matches": len(page.messages),
            "returned_accessible_matches": len(matches),
            "total_results_estimate": page.total_results,
            "next_offset": page.next_offset,
            "next_cursor": (
                _search_cursor(page.next_offset, cursor_scope)
                if page.next_offset is not None
                else None
            ),
            "snapshot_before_message_id": max_id,
            "deep_historical_indexing": page.deep_historical_indexing,
            "offset_limit_reached": page.offset_limit_reached,
            "reached_current_indexed_end": (
                page.next_offset is None
                and not page.deep_historical_indexing
                and not page.offset_limit_reached
            ),
            "total_may_change_while_messages_are_created_or_deleted": True,
        }
    return result


def _cursor_state(
    value: Any,
    base_scope: Mapping[str, Any],
    default_max_id: int,
) -> tuple[int, int, dict[str, Any]]:
    if value is None:
        scope = {**base_scope, "effective_max_id": default_max_id}
        return 0, default_max_id, scope
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid Discord search cursor")
    parts = value.split(".")
    if (
        len(parts) != 4
        or parts[0] != "v1"
        or not parts[1].isdigit()
        or not parts[2].isdigit()
    ):
        raise ValueError("Invalid Discord search cursor")
    offset = int(parts[1])
    max_id = int(parts[2])
    scope = {**base_scope, "effective_max_id": max_id}
    if (
        not 0 <= offset <= 9_975
        or max_id <= 0
        or parts[3] != _scope_digest(scope)
    ):
        raise ValueError("Invalid Discord search cursor")
    return offset, max_id, scope


def _search_cursor(offset: int, scope: Mapping[str, Any]) -> str:
    return (
        f"v1.{offset}.{scope['effective_max_id']}.{_scope_digest(scope)}"
    )


def _history_cursor_state(
    value: Any,
    base_scope: Mapping[str, Any],
    default_before_id: int,
) -> tuple[int, dict[str, Any]]:
    if value is None:
        if default_before_id <= base_scope["after_id"]:
            raise ValueError("Invalid Discord history window")
        scope = {**base_scope, "effective_before_id": default_before_id}
        return default_before_id, scope
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid Discord history cursor")
    parts = value.split(".")
    if (
        len(parts) != 4
        or parts[0] != "h1"
        or not parts[1].isdigit()
        or not parts[2].isdigit()
    ):
        raise ValueError("Invalid Discord history cursor")
    page_before_id = int(parts[1])
    effective_before_id = int(parts[2])
    scope = {**base_scope, "effective_before_id": effective_before_id}
    if (
        not base_scope["after_id"] < page_before_id < effective_before_id
        or parts[3] != _scope_digest(scope)
    ):
        raise ValueError("Invalid Discord history cursor")
    return page_before_id, scope


def _history_cursor(before_id: int, scope: Mapping[str, Any]) -> str:
    return (
        f"h1.{before_id}.{scope['effective_before_id']}."
        f"{_scope_digest(scope)}"
    )


def _scope_digest(scope: Mapping[str, Any]) -> str:
    payload = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _request_message_boundary(context: AgentRequestContext) -> int:
    message_id = getattr(context.source_message, "id", None)
    if type(message_id) is int and message_id > 0:
        return message_id
    created_at = getattr(context.source_message, "created_at", None)
    if not isinstance(created_at, datetime):
        raise ValueError("Missing Discord search snapshot boundary")
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return discord.utils.time_snowflake(created_at.astimezone(timezone.utc))


def _request_time(context: AgentRequestContext) -> datetime:
    created_at = getattr(context.source_message, "created_at", None)
    if not isinstance(created_at, datetime):
        raise ValueError("Missing Discord request time")
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc)


def _search_date(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


async def find_discord_channels(context: AgentRequestContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    query = str(arguments["query"]).strip().casefold()
    numeric = query.removeprefix("<#").removesuffix(">")
    if numeric.isdecimal():
        channel = await accessible_message_channel(context, int(numeric))
        if channel is None:
            return {"channels": [], "matched_count": 0, "truncated": False}
        context.state.source_channels.add(channel.id)
        return {"channels": [{"channel_id": channel.id, "name": channel.name}], "matched_count": 1, "truncated": False}
    channels = []
    for channel_id in searchable_channel_ids(context, limit=None):
        channel = context.guild.get_channel_or_thread(channel_id)
        name = str(getattr(channel, "name", ""))
        if str(channel_id) == numeric or query.lstrip("#") in name.casefold():
            channels.append({"channel_id": channel_id, "name": name})
    context.state.source_channels.update(item["channel_id"] for item in channels[:20])
    return {"channels": channels[:20], "matched_count": len(channels), "truncated": len(channels) > 20}


async def read_message_context(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    channel_id = positive_int(arguments.get("channel_id"))
    message_id = positive_int(arguments.get("message_id"))
    if channel_id is None or message_id is None:
        return {"error": "Valid channel_id and message_id values are required."}
    channel = await accessible_message_channel(context, channel_id)
    if channel is None:
        return {"error": "The asker cannot access that conversation."}
    history = getattr(channel, "history", None)
    if not callable(history):
        return {"error": "That Discord location does not expose message history."}
    limit = bounded_int(
        arguments.get("limit"),
        default=15,
        minimum=3,
        maximum=CONTEXT_MESSAGE_LIMIT,
    )
    messages = [
        message
        async for message in history(
            limit=limit,
            around=discord.Object(id=message_id),
            oldest_first=True,
        )
    ]
    if await accessible_message_channel(context, channel_id) is None:
        return {"error": "The asker cannot access that conversation."}
    context.state.source_channels.add(channel_id)
    return {
        "channel_id": channel_id,
        "channel": getattr(channel, "name", str(channel_id)),
        "requested_message_id": message_id,
        "messages": [
            discord_message_evidence(context.guild.id, message)
            for message in messages
        ],
    }


def searchable_channel_ids(context: AgentRequestContext, *, limit: int | None = 500) -> tuple[int, ...]:
    bot_member = context.guild.me
    if bot_member is None:
        return ()
    channels = [*context.guild.channels, *context.guild.threads]
    allowed: list[int] = []
    for channel in channels:
        if isinstance(
            channel,
            (discord.CategoryChannel, discord.StageChannel, discord.VoiceChannel),
        ):
            continue
        permissions_for = getattr(channel, "permissions_for", None)
        if not callable(permissions_for):
            continue
        asker_permissions = permissions_for(context.member)
        bot_permissions = permissions_for(bot_member)
        if not (
            asker_permissions.view_channel
            and asker_permissions.read_message_history
            and bot_permissions.view_channel
            and bot_permissions.read_message_history
        ):
            continue
        if isinstance(channel, discord.Thread) and channel.is_private():
            get_member = getattr(channel, "get_member", None)
            if not callable(get_member) or get_member(context.member.id) is None:
                continue
        allowed.append(channel.id)
        if limit is not None and len(allowed) >= limit:
            break
    return tuple(allowed)


def discord_message_evidence(guild_id: int, message: discord.Message) -> dict[str, Any]:
    content = message_text(message)
    return {
        "message_id": message.id,
        "author_id": message.author.id,
        "author": message.author.display_name,
        "timestamp": message.created_at.isoformat(),
        "content": bounded_text(content, 1_600),
        "source": jump_url(guild_id, message.channel.id, message.id),
    }


def jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
