"""Discord history tools with asker-specific permission enforcement."""

from __future__ import annotations

from typing import Any
from typing import Mapping

import discord

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..models import AgentRequestContext
from ..message_content import message_text
from ..models import RegisteredAgentTool
from .shared import bounded_int
from .shared import bounded_text
from .shared import positive_int


SEARCH_RESULT_LIMIT = 10
CONTEXT_MESSAGE_LIMIT = 50


def discord_tools() -> tuple[RegisteredAgentTool, ...]:
    return (
        RegisteredAgentTool(
            AgentToolDefinition(
                name="search_discord_messages",
                description=(
                    "Search accessible server message history for specific words or phrases. "
                    "Use only when the request needs historical Discord evidence, not for banter "
                    "or facts already present in the supplied local context."
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
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            search_discord_messages,
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="read_message_context",
                description=(
                    "Read messages surrounding one accessible Discord search result so its "
                    "meaning can be assessed in context."
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


async def search_discord_messages(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"error": "A non-empty search query is required."}
    limit = bounded_int(
        arguments.get("limit"),
        default=5,
        minimum=1,
        maximum=SEARCH_RESULT_LIMIT,
    )
    channel_ids = searchable_channel_ids(context)
    if not channel_ids:
        return {"query": query, "matches": [], "search_is_exhaustive": False}
    raw_results = await context.message_search.search(
        guild_id=context.guild.id,
        content=query,
        limit=25,
        channel_ids=channel_ids,
    )
    matches: list[dict[str, Any]] = []
    for result in raw_results:
        channel = await accessible_message_channel(context, result.channel_id)
        if channel is None:
            continue
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
    return {
        "query": query,
        "matches": matches,
        "search_is_exhaustive": False,
    }


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
    return {
        "channel_id": channel_id,
        "channel": getattr(channel, "name", str(channel_id)),
        "requested_message_id": message_id,
        "messages": [
            discord_message_evidence(context.guild.id, message)
            for message in messages
        ],
    }


def searchable_channel_ids(context: AgentRequestContext) -> tuple[int, ...]:
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
        if len(allowed) >= 500:
            break
    return tuple(allowed)


async def accessible_message_channel(
    context: AgentRequestContext,
    channel_id: int,
) -> Any | None:
    channel = context.guild.get_channel_or_thread(channel_id)
    if channel is None:
        try:
            channel = await context.bot.fetch_channel(channel_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
    channel_guild = getattr(channel, "guild", None)
    if getattr(channel_guild, "id", None) != context.guild.id:
        return None
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return None
    asker_permissions = permissions_for(context.member)
    bot_member = context.guild.me
    if bot_member is None:
        return None
    bot_permissions = permissions_for(bot_member)
    if not (
        asker_permissions.view_channel
        and asker_permissions.read_message_history
        and bot_permissions.view_channel
        and bot_permissions.read_message_history
    ):
        return None
    if isinstance(channel, discord.Thread) and channel.is_private():
        try:
            await channel.fetch_member(context.member.id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
    return channel


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
