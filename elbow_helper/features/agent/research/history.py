"""Discord history tools with asker-specific permission enforcement."""

from __future__ import annotations

from typing import Any
from typing import Mapping
from datetime import datetime, timezone
import hashlib
import json

import discord


from ..access import accessible_message_channel
from ..models import AgentRequestContext
from ..text import message_text
from ..capabilities.validation import bounded_int
from ..capabilities.validation import bounded_text
from ..capabilities.validation import positive_int


SEARCH_RESULT_LIMIT = 25
CONTEXT_MESSAGE_LIMIT = 50
HISTORY_PAGE_LIMIT = 25
INTERACTIVE_HISTORY_PAGE_LIMIT = 100
HISTORY_MESSAGE_CHARACTER_BUDGET = 44_000


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
        after = search_date(arguments.get("after"))
        before = search_date(arguments.get("before"))
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
        page_before_id, cursor_scope = _historycursor_state(
            arguments.get("cursor"), base_scope,
            requested_before_id or request_message_boundary(context),
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
        minimum=1, maximum=INTERACTIVE_HISTORY_PAGE_LIMIT,
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
    messages = []
    message_characters = 0
    scanned_messages = 0
    next_before_id = page.next_before_id
    for message in page.messages:
        if author_id is not None and message.author_id != author_id:
            scanned_messages += 1
            continue
        entry = {
            "message_id": message.message_id,
            "channel_id": channel_id,
            "channel": getattr(channel, "name", str(channel_id)),
            "author_id": message.author_id,
            "author": message.author_name,
            "timestamp": message.timestamp,
            "content": bounded_text(message.content, 1_600),
            "source": jump_url(context.guild.id, channel_id, message.message_id),
        }
        entry_characters = len(json.dumps(entry, ensure_ascii=False, separators=(",", ":"))) + 1
        if messages and message_characters + entry_characters > HISTORY_MESSAGE_CHARACTER_BUDGET:
            # Discord pages are newest first. Resume immediately before the
            # last examined message, including filtered-out authors, so no
            # omitted matching message is skipped when a large page is cut.
            next_before_id = page.messages[scanned_messages - 1].message_id
            break
        messages.append(entry)
        message_characters += entry_characters
        scanned_messages += 1
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
            "scanned_messages_in_window": scanned_messages,
            "returned_messages": len(messages),
            "next_cursor": (
                _history_cursor(next_before_id, cursor_scope)
                if next_before_id is not None else None
            ),
            "reached_requested_start": next_before_id is None,
            "covers_currently_available_messages_only": True,
        },
    }


def cursor_state(
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


def search_cursor(offset: int, scope: Mapping[str, Any]) -> str:
    return (
        f"v1.{offset}.{scope['effective_max_id']}.{_scope_digest(scope)}"
    )


def _historycursor_state(
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


def request_message_boundary(context: AgentRequestContext) -> int:
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


def search_date(value: Any) -> datetime | None:
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
    try:
        after = search_date(arguments.get("after"))
        before = search_date(arguments.get("before"))
    except ValueError:
        return {"error": "invalid_period"}
    if after is not None or before is not None:
        if after is None or before is None or after >= before:
            return {"error": "invalid_period"}
        lower = discord.utils.time_snowflake(after) - 1
        upper = discord.utils.time_snowflake(before)
        if not lower < message_id < upper:
            return {"error": "message_out_of_period"}
        preceding = [message async for message in history(
            limit=limit // 2 + 1, after=discord.Object(id=lower),
            before=discord.Object(id=message_id + 1), oldest_first=False,
        )]
        following = [message async for message in history(
            limit=limit - len(preceding), after=discord.Object(id=message_id),
            before=discord.Object(id=upper), oldest_first=True,
        )]
        messages = [*reversed(preceding), *following]
    else:
        messages = [message async for message in history(
            limit=limit, around=discord.Object(id=message_id), oldest_first=True,
        )]
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
