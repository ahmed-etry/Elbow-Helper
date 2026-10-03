"""Agent rollout, source-channel, and retained-evidence access."""

from __future__ import annotations

from typing import Any
from functools import wraps

import discord

from elbow_helper.configuration.roles import CORE, CWL_HELPERS, LEAD, LEAD_PLUS, RECRUITERS

from .models import AgentRequestContext
from .wording import ACTION_UNAVAILABLE


class AgentAccessLost(RuntimeError):
    """The requester can no longer use this agent conversation."""


class LookupAccessDenied(RuntimeError):
    """The requester lacks a new lookup's role group."""

    def __init__(self, requirements):
        super().__init__(ACTION_UNAVAILABLE)
        self.requirements = frozenset(requirements)


def require_lookup_access(context, requirements):
    if not has_access_requirements(context.guild, context.member.id, requirements):
        if requirements <= context.state.required_access:
            raise AgentAccessLost("Retained source role access is no longer available")
        raise LookupAccessDenied(requirements)


ACCESS_LEAD = "lead"
ACCESS_LEAD_PLUS = "lead_plus"
ACCESS_CORE = "core"
ACCESS_RECRUITER_OR_CORE = "recruiter_or_core"
ACCESS_LEAD_PLUS_OR_CWL_HELPER = "lead_plus_or_cwl_helper"
ACCESS_ROLE_SETS = {
    ACCESS_LEAD: LEAD,
    ACCESS_LEAD_PLUS: LEAD_PLUS,
    ACCESS_CORE: CORE,
    ACCESS_RECRUITER_OR_CORE: RECRUITERS | CORE,
    ACCESS_LEAD_PLUS_OR_CWL_HELPER: LEAD_PLUS | CWL_HELPERS,
}
KNOWN_ACCESS_REQUIREMENTS = frozenset(ACCESS_ROLE_SETS)
AGENT_ROLLOUT_ROLE_IDS = CORE


def has_agent_entry_access(member: Any) -> bool:
    """Keep current beta eligibility separate from the agent's identity."""
    return any(
        getattr(role, "id", None) in AGENT_ROLLOUT_ROLE_IDS
        for role in getattr(member, "roles", ())
    )


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
    return all(role_ids & ACCESS_ROLE_SETS[level] for level in requirements)


def lookup_level(level: str):
    """Check a new read locally, then retain its access with the evidence."""
    def decorate(handler):
        @wraps(handler)
        async def read(context, arguments):
            await require_evidence_access(context)
            if not has_access_requirements(context.guild, context.member.id, {level}):
                return {"error": ACTION_UNAVAILABLE, "required_access": [level]}
            previous = dict(context.state.reports)
            result = await handler(context, arguments)
            if not has_access_requirements(context.guild, context.member.id, {level}):
                context.state.reports.clear()
                context.state.reports.update(previous)
                return {"error": ACTION_UNAVAILABLE, "required_access": [level]}
            if "error" not in result:
                context.state.required_access.add(level)
                for report_id, report in context.state.reports.items():
                    if previous.get(report_id) is not report:
                        context.state.report_sources[report_id] = frozenset(context.state.source_channels)
                        context.state.report_access_requirements[report_id] = frozenset(context.state.required_access)
            return result
        return read
    return decorate


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
    if member is None or not has_agent_entry_access(member):
        raise AgentAccessLost("Agent access is no longer available")
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


async def require_evidence_access(context: AgentRequestContext) -> dict[int, Any]:
    """Fail closed before reusing evidence whose sources became inaccessible."""
    require_access(context.guild, context.member.id, context.source_message.channel)
    require_access_requirements(
        context.guild, context.member.id, context.state.required_access,
    )
    sources = {}
    for channel_id in tuple(context.state.source_channels):
        channel = await accessible_message_channel(context, channel_id)
        if channel is None:
            raise AgentAccessLost(f"Conversation evidence access is no longer available: channel={channel_id}")
        sources[channel_id] = channel
    return sources
