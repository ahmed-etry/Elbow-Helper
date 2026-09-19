"""Current Core membership and source-channel access for agent requests."""

from __future__ import annotations

from typing import Any

import discord

from elbow_helper.configuration.roles import CORE, LEAD, LEAD_PLUS

from .models import AgentRequestContext


class AgentAccessLost(RuntimeError):
    """The requester can no longer use this agent conversation."""


ACCESS_LEAD = "lead"
ACCESS_LEAD_PLUS = "lead_plus"
KNOWN_ACCESS_REQUIREMENTS = frozenset({ACCESS_LEAD, ACCESS_LEAD_PLUS})


def has_access_requirements(
    guild: discord.Guild,
    member_id: int,
    requirements: frozenset[str] | set[str],
) -> bool:
    """Evaluate retained-data role requirements against current membership."""
    if not requirements <= KNOWN_ACCESS_REQUIREMENTS:
        return False
    member = guild.get_member(member_id)
    if member is None:
        return False
    role_ids = {role.id for role in member.roles}
    return bool(
        (ACCESS_LEAD not in requirements or role_ids & LEAD)
        and (ACCESS_LEAD_PLUS not in requirements or role_ids & LEAD_PLUS)
    )


def require_access_requirements(
    guild: discord.Guild,
    member_id: int,
    requirements: frozenset[str] | set[str],
) -> None:
    if not has_access_requirements(guild, member_id, requirements):
        raise AgentAccessLost("Required source role access is no longer available")


def require_access(
    guild: discord.Guild,
    member_id: int,
    channel: discord.abc.GuildChannel | discord.Thread,
) -> discord.Member:
    member = guild.get_member(member_id)
    if member is None or not any(role.id in CORE for role in member.roles):
        raise AgentAccessLost("Core access is no longer available")
    bot_member = guild.me
    if bot_member is None:
        raise AgentAccessLost("Bot membership is unavailable")
    for actor in (member, bot_member):
        permissions = channel.permissions_for(actor)
        if not (permissions.view_channel and permissions.read_message_history):
            raise AgentAccessLost("Conversation access is no longer available")
    return member


async def accessible_message_channel(
    context: AgentRequestContext,
    channel_id: int,
) -> Any | None:
    """Resolve a source using the asker's current membership and permissions."""
    channel = context.guild.get_channel_or_thread(channel_id)
    if channel is None:
        try:
            channel = await context.bot.fetch_channel(channel_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
    return channel if await can_access_message_channel(context, channel) else None


async def can_access_message_channel(
    context: AgentRequestContext,
    channel: Any,
) -> bool:
    """Check a resolved channel without widening access through another lookup."""
    if getattr(getattr(channel, "guild", None), "id", None) != context.guild.id:
        return False
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return False
    member = context.guild.get_member(context.member.id)
    bot_member = context.guild.me
    if member is None or bot_member is None:
        return False
    for actor in (member, bot_member):
        permissions = permissions_for(actor)
        if not (permissions.view_channel and permissions.read_message_history):
            return False
    if isinstance(channel, discord.Thread) and channel.is_private():
        try:
            await channel.fetch_member(member.id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return False
    return True


async def require_evidence_access(context: AgentRequestContext) -> None:
    """Fail closed before reusing evidence whose sources became inaccessible."""
    require_access(context.guild, context.member.id, context.source_message.channel)
    require_access_requirements(
        context.guild, context.member.id, context.state.required_access,
    )
    for channel_id in tuple(context.state.source_channels):
        if await accessible_message_channel(context, channel_id) is None:
            raise AgentAccessLost("Conversation evidence access is no longer available")
