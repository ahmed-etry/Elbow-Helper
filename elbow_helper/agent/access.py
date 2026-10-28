"""Agent rollout, source-channel, and retained-evidence access."""

from __future__ import annotations

from typing import Any
from functools import wraps
import re

import discord

from elbow_helper.configuration.roles import (
    AGENT_TESTER_ROLE_ID, CORE, CWL_HELPERS, EXAMINERS, LEAD, LEAD_PLUS, RECRUITERS,
    SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID,
)

from .models import AgentRequestContext


class AgentAccessLost(RuntimeError):
    """The requester can no longer use this agent conversation."""


class LookupAccessDenied(RuntimeError):
    """The requester lacks a new lookup's role group."""

    def __init__(self, requirements):
        super().__init__("This needs " + ", ".join(sorted(requirements)) + " access.")
        self.requirements = frozenset(requirements)


def require_lookup_access(context, requirements):
    if not requirements:
        return
    if not has_lookup_access(context, requirements):
        if requirements <= context.state.required_access:
            raise AgentAccessLost("Retained source role access is no longer available")
        raise LookupAccessDenied(requirements)


def has_lookup_access(context, requirements):
    """A read needs both requester authorization and the active evidence scope."""
    from .disclosure import exclude_source
    if not has_reader_access_requirements(context.guild, context.member.id, requirements):
        return False
    scope = getattr(context.state, "scope", None)
    if scope is not None and not requirements <= scope.levels:
        for level in requirements - scope.levels:
            exclude_source(context, "access:" + level, "role_scope", relevant=True)
        return False
    return True


ACCESS_LEAD = "lead"
ACCESS_LEAD_PLUS = "lead_plus"
ACCESS_CORE = "core"
ACCESS_RECRUITER = "recruiter"
ACCESS_CWL_HELPER = "cwl_helper"
ACCESS_EXAMINER = "examiner"
ACCESS_RECRUITER_OR_CORE = "recruiter_or_core"
ACCESS_LEAD_OR_RECRUITER = "lead_or_recruiter"
ACCESS_LEAD_PLUS_OR_CWL_HELPER = "lead_plus_or_cwl_helper"
ACCESS_LEAD_PLUS_OR_CORE = "lead_plus_or_core"
ACCESS_RECRUITMENT = "recruitment"
ACCESS_PROMOTION = "promotion"
ACCESS_CWL = "cwl"
ACCESS_ROLE_SETS = {
    ACCESS_LEAD: LEAD,
    ACCESS_LEAD_PLUS: LEAD_PLUS,
    ACCESS_CORE: CORE,
    ACCESS_RECRUITER: RECRUITERS | LEAD_PLUS,
    ACCESS_CWL_HELPER: CWL_HELPERS | LEAD_PLUS,
    ACCESS_EXAMINER: EXAMINERS | LEAD_PLUS,
    ACCESS_RECRUITER_OR_CORE: RECRUITERS | CORE,
    ACCESS_LEAD_OR_RECRUITER: LEAD | RECRUITERS,
    ACCESS_LEAD_PLUS_OR_CWL_HELPER: LEAD_PLUS | CWL_HELPERS,
    # Source audiences include Core observers without changing the narrower
    # department knowledge and command requirements above.
    ACCESS_LEAD_PLUS_OR_CORE: LEAD_PLUS | CORE,
    ACCESS_RECRUITMENT: RECRUITERS | LEAD_PLUS | CORE,
    ACCESS_PROMOTION: EXAMINERS | LEAD_PLUS | CORE,
    ACCESS_CWL: CWL_HELPERS | LEAD_PLUS | CORE,
}
ACCESS_VIEW_AUDIT_LOG = "View Audit Log"
READ_ACCESS_ROLE_SETS = {
    level: roles | {SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID} if LEAD <= roles else roles
    for level, roles in ACCESS_ROLE_SETS.items()
}
KNOWN_ACCESS_REQUIREMENTS = frozenset(ACCESS_ROLE_SETS) | {ACCESS_VIEW_AUDIT_LOG}
AGENT_ROLLOUT_ROLE_IDS = LEAD_PLUS | CORE | {AGENT_TESTER_ROLE_ID}


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
    return _has_role_requirements(guild, member_id, requirements, ACCESS_ROLE_SETS)


def has_reader_access_requirements(guild, member_id, requirements):
    """Sleeping leaders remain cleared readers without gaining action permissions."""
    return _has_role_requirements(guild, member_id, requirements, READ_ACCESS_ROLE_SETS)


def leadership_asker(context):
    """Share the leadership allowance, including sleeping leaders, across agent limits."""
    return has_reader_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD_PLUS_OR_CORE},
    )


def _has_role_requirements(guild, member_id, requirements, role_sets):
    if not requirements <= KNOWN_ACCESS_REQUIREMENTS:
        return False
    member = guild.get_member(member_id)
    if member is None:
        return False
    role_ids = {role.id for role in getattr(member, "roles", ())}
    if ACCESS_VIEW_AUDIT_LOG in requirements:
        for actor in (member, guild.me):
            if actor is None or not getattr(
                getattr(actor, "guild_permissions", None), "view_audit_log", False,
            ):
                return False
    return all(role_ids & role_sets[level]
               for level in requirements if level != ACCESS_VIEW_AUDIT_LOG)


def lookup_level(level: str):
    """Check a new read locally, then retain its access with the evidence."""
    def decorate(handler):
        @wraps(handler)
        async def read(context, arguments):
            await require_evidence_access(context)
            if not has_lookup_access(context, {level}):
                return {"error": f"This needs {level} access.", "required_access": [level]}
            previous = dict(context.state.reports)
            result = await handler(context, arguments)
            if not has_lookup_access(context, {level}):
                context.state.reports.clear()
                context.state.reports.update(previous)
                return {"error": f"This needs {level} access.", "required_access": [level]}
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
    *, relevant: bool = True,
) -> Any | None:
    """Resolve a source using the asker's current membership and permissions."""
    channel = context.guild.get_channel_or_thread(channel_id)
    if channel is None:
        try:
            channel = await context.bot.fetch_channel(channel_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
    return channel if await can_access_message_channel(context, channel, relevant=relevant) else None


async def can_access_message_channel(
    context: AgentRequestContext,
    channel: Any,
    *, relevant: bool = True,
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
    permissions = permissions_for(bot_member)
    if not (permissions.view_channel and permissions.read_message_history):
        return False
    from .disclosure import request_scope, source_access_levels, personal_source_allowed, exclude_source
    scope = await request_scope(context)
    if channel.id == scope.destination_id:
        return True
    levels = source_access_levels(
        channel, context.guild, examination_queries=getattr(context, "examination_queries", None),
    )
    if levels is not None:
        allowed_levels = {level for level in levels & scope.levels
                          if has_reader_access_requirements(context.guild, member.id, {level})}
        if not levels or allowed_levels:
            if levels:
                context.state.source_access.setdefault(channel.id, frozenset({min(allowed_levels)}))
            return True
        # An applicant's explicit ticket grant is personal access, independent
        # of recruitment staff roles. Preserve the destination audience check.
        from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
        if getattr(channel, 'category_id', None) == RECRUITMENT_TICKET_CATEGORY:
            grants = getattr(channel, 'overwrites', {})
            explicit = any(target.id == member.id and overwrite.view_channel is True
                           for target, overwrite in grants.items())
            personal = permissions_for(member)
            if explicit and personal.view_channel and personal.read_message_history:
                destination = getattr(context, 'delivery_channel', None) or context.source_message.channel
                if scope.requester or await personal_source_allowed(
                        destination, channel, context.guild,
                        thread_members=getattr(context, 'disclosure_thread_members', None)):
                    context.state.source_access[channel.id] = frozenset()
                    return True
                exclude_source(context, channel.id, 'per_person', relevant=relevant)
                return False
        exclude_source(context, channel.id, "role_scope", relevant=relevant and any(
            has_reader_access_requirements(context.guild, member.id, {level}) for level in levels
        ))
        return False
    permissions = permissions_for(member)
    if not (permissions.view_channel and permissions.read_message_history):
        return False
    if (isinstance(channel, discord.Thread) and channel.is_private()
            and not getattr(permissions, "manage_threads", False)):
        try:
            await channel.fetch_member(member.id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return False
    if scope.requester:
        return True
    destination = getattr(context, "delivery_channel", None) or context.source_message.channel
    allowed = await personal_source_allowed(
        destination, channel, context.guild,
        thread_members=getattr(context, "disclosure_thread_members", None),
    )
    if not allowed:
        exclude_source(context, channel.id, "per_person", relevant=relevant)
    return allowed


async def require_evidence_access(context: AgentRequestContext) -> dict[int, Any]:
    """Fail closed before reusing evidence whose sources became inaccessible."""
    require_access(context.guild, context.member.id, context.source_message.channel)
    from .disclosure import request_scope
    scope = await request_scope(context, refresh=True)
    require_access_requirements(context.guild, context.member.id, context.state.help_requester_access)
    if not has_reader_access_requirements(context.guild, context.member.id,
            context.state.required_access | context.state.knowledge_title_access):
        raise AgentAccessLost("Required source role access is no longer available")
    if not (context.state.required_access | context.state.knowledge_title_access) <= scope.levels:
        raise AgentAccessLost("Retained evidence is outside the destination scope")
    sources = {}
    for channel_id in tuple(context.state.source_channels):
        channel = await accessible_message_channel(context, channel_id, relevant=False)
        if channel is None:
            raise AgentAccessLost(f"Conversation evidence access is no longer available: channel={channel_id}")
        sources[channel_id] = channel
        context.state.required_access.update(context.state.source_access.get(channel_id, ()))
    if not context.state.required_access <= scope.levels:
        raise AgentAccessLost("Retained evidence is outside the destination scope")
    return sources


async def assert_answer_scope(context, text=""):
    """Last cheap bug guard. Violations abort delivery, never become a private notice."""
    await require_evidence_access(context)
    for guild_id, channel_id in re.findall(
        r"https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)(?:/\d+)?", text or "",
    ):
        if (int(guild_id) != context.guild.id
                or await accessible_message_channel(context, int(channel_id), relevant=False) is None):
            raise AgentAccessLost("Out-of-scope answer citation")
        context.state.source_channels.add(int(channel_id))
        context.state.required_access.update(context.state.source_access.get(int(channel_id), ()))
