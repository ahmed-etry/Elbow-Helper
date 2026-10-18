"""Source audiences and retained-evidence disclosure."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import discord

from .access import (
    ACCESS_ROLE_SETS, ACCESS_VIEW_AUDIT_LOG, KNOWN_ACCESS_REQUIREMENTS,
    accessible_message_channel, has_access_requirements,
)
from .models import AgentRequestContext

LOGGER = logging.getLogger(__name__)


def _read_audience_signature(channel: Any) -> tuple[tuple[int, bool | None, bool | None], ...] | None:
    """Describe role/member read overwrites without relying on cached members."""
    if isinstance(channel, discord.Thread):
        return None
    overwrites = getattr(channel, "overwrites", None)
    if not isinstance(overwrites, Mapping):
        return None
    signature = []
    for target, overwrite in overwrites.items():
        target_id = getattr(target, "id", None)
        view = getattr(overwrite, "view_channel", None)
        history = getattr(overwrite, "read_message_history", None)
        if type(target_id) is not int or view not in (True, False, None) or history not in (True, False, None):
            return None
        signature.append((target_id, view, history))
    return tuple(sorted(signature))


def _public_read_source(channel: Any, guild: discord.Guild) -> bool:
    """A source readable by everyone without role/member read denials."""
    if isinstance(channel, discord.Thread) and channel.is_private():
        return False
    source = channel.parent if isinstance(channel, discord.Thread) else channel
    signature = _read_audience_signature(source)
    default_role = getattr(guild, "default_role", None)
    if signature is None or default_role is None:
        return False
    permissions = source.permissions_for(default_role)
    return bool(
        permissions.view_channel and permissions.read_message_history
        and all(view is not False and history is not False for _, view, history in signature)
    )


def _source_audience_contains_destination(
    source: Any, destination: Any, guild: discord.Guild,
) -> bool:
    source_id = getattr(source, "id", None)
    destination_id = getattr(destination, "id", None)
    if type(source_id) is not int or type(destination_id) is not int:
        return False
    if source_id == destination_id:
        return True
    if _public_read_source(source, guild):
        return True
    source_signature = _read_audience_signature(source)
    destination_signature = _read_audience_signature(destination)
    structurally_equal = bool(
        source_signature is not None
        and destination_signature is not None
        and getattr(source, "type", None) is not None
        and getattr(source, "type", None) == getattr(destination, "type", None)
        and getattr(getattr(source, "guild", None), "id", None)
        == getattr(getattr(destination, "guild", None), "id", None)
        and source_signature == destination_signature
    )
    if not structurally_equal:
        return False
    roles = getattr(guild, "roles", None)
    default_role = getattr(guild, "default_role", None)
    if roles is None or default_role is None or default_role not in roles:
        return False
    for role in roles:
        if getattr(role, "managed", False):
            continue
        destination_permissions = destination.permissions_for(role)
        if not destination_permissions.view_channel:
            continue
        source_permissions = source.permissions_for(role)
        if not (source_permissions.view_channel and source_permissions.read_message_history):
            return False
    return True


def _destination_satisfies_role_requirements(
    channel: Any, guild: discord.Guild, requirements: frozenset[str] | set[str],
) -> bool:
    if not requirements:
        return True
    if ACCESS_VIEW_AUDIT_LOG in requirements:
        return False
    if not requirements <= KNOWN_ACCESS_REQUIREMENTS:
        return False
    destination = channel.parent if isinstance(channel, discord.Thread) else channel
    roles = getattr(guild, "roles", None)
    default_role = getattr(guild, "default_role", None)
    signature = _read_audience_signature(destination)
    if roles is None or default_role is None or signature is None:
        return False
    # The owner bypasses channel overwrites, even without an administrator role.
    owner_id = getattr(guild, "owner_id", None)
    if type(owner_id) is not int or not has_access_requirements(guild, owner_id, requirements):
        return False
    allowed_ids = set.intersection(*(set(ACCESS_ROLE_SETS[level]) for level in requirements))
    known_roles = {getattr(role, "id", None) for role in roles}
    if not known_roles or getattr(default_role, "id", None) not in known_roles:
        return False
    if destination.permissions_for(default_role).view_channel:
        return False
    for role in roles:
        if getattr(role, "managed", False):
            continue
        if destination.permissions_for(role).view_channel and role.id not in allowed_ids:
            return False
    for target, overwrite in destination.overwrites.items():
        if overwrite.view_channel is True and target.id not in known_roles:
            # A member-specific grant is safe only when that member can be
            # resolved and satisfies every requirement under the usual checks.
            if not has_access_requirements(guild, target.id, requirements):
                return False
    return True


async def can_disclose_provenance(
    context: AgentRequestContext, source_channels: frozenset[int] | set[int],
    required_access: frozenset[str] | set[str],
) -> bool:
    """Prove that every current destination viewer may see all source evidence."""
    destination = getattr(context, "delivery_channel", None) or getattr(
        getattr(context, "source_message", None), "channel", None,
    )
    if destination is None or not has_access_requirements(
        context.guild, context.member.id, required_access,
    ):
        return False
    sources = {}
    for channel_id in source_channels:
        source = await accessible_message_channel(context, channel_id)
        if source is None:
            return False
        sources[channel_id] = source
    return await can_show(
        destination, sources, required_access, context.guild,
        thread_members=getattr(context, "disclosure_thread_members", None),
        request_id=getattr(context.source_message, "id", None),
    )


def _resolved_sources_disclosable(
    destination: Any, guild: discord.Guild, sources: Mapping[int, Any],
    required_access: frozenset[str] | set[str],
) -> bool:
    return bool(
        getattr(getattr(destination, "guild", None), "id", None)
        == getattr(guild, "id", None)
        and _destination_satisfies_role_requirements(
            destination, guild, required_access,
        )
        and all(
            _source_audience_contains_destination(source, destination, guild)
            for source in sources.values()
        )
    )


async def _thread_member_ids(thread, cache):
    if thread.id not in cache:
        try:
            cache[thread.id] = frozenset(member.id for member in await thread.fetch_members())
        except (discord.DiscordException, OSError, TimeoutError):
            cache[thread.id] = None
    return cache[thread.id]


async def _destination_viewers(destination, guild, cache):
    if isinstance(destination, discord.Thread):
        if destination.is_private():
            identifiers = await _thread_member_ids(destination, cache)
            if identifiers is None:
                return None
            members = tuple(guild.get_member(identifier) for identifier in identifiers)
            if any(member is None for member in members):
                return None
            # Manage Threads also grants readership without joining the thread.
            moderators = tuple(member for member in guild.members
                               if (permissions := destination.permissions_for(member)).view_channel
                               and getattr(permissions, "manage_threads", False))
            return (*members, *moderators)
        destination = destination.parent
    if destination is None:
        return None
    return tuple(member for member in guild.members
                 if destination.permissions_for(member).view_channel)


async def can_show(
    destination: Any, sources: Mapping[int, Any],
    access_levels: frozenset[str] | set[str], guild: discord.Guild,
    *, thread_members: dict[int, frozenset[int] | None] | None = None,
    request_id: int | None = None,
) -> bool:
    """Compare the actual destination audience with every source's readership."""
    def decide(allowed, reason, *, source_id=None, member_id=None):
        LOGGER.info(
            "Agent audience decision: request=%s channel=%s allowed=%s reason=%s sources=%s "
            "levels=%s source=%s member=%s",
            request_id, getattr(destination, "id", None), allowed, reason, sorted(sources),
            sorted(access_levels), source_id, member_id,
        )
        return allowed

    if isinstance(destination, discord.DMChannel):
        recipient = destination.recipient
        allowed = recipient is not None and await can_show_in_dm(
            guild, recipient.id, sources, access_levels,
        )
        return decide(allowed, "dm_recipient_authorized" if allowed else "dm_recipient_denied")
    if ACCESS_VIEW_AUDIT_LOG in access_levels:
        return decide(False, "audit_log_requires_private_delivery")
    if (getattr(getattr(destination, "guild", None), "id", None) != guild.id
            or not access_levels <= KNOWN_ACCESS_REQUIREMENTS
            or any(getattr(getattr(source, "guild", None), "id", None) != guild.id
                   for source in sources.values())):
        return decide(False, "unknown_access_or_guild_boundary")
    bot_member = guild.me
    if bot_member is None:
        return decide(False, "bot_membership_unavailable")
    for source in sources.values():
        permissions = source.permissions_for(bot_member)
        if not (permissions.view_channel and permissions.read_message_history):
            return decide(False, "bot_source_read_denied", source_id=source.id)
    if not getattr(guild, "chunked", False):
        allowed = _resolved_sources_disclosable(destination, guild, sources, access_levels)
        return decide(allowed, "permission_structure_proven" if allowed else "incomplete_member_cache_unproven")
    cache = thread_members if thread_members is not None else {}
    viewers = await _destination_viewers(destination, guild, cache)
    if viewers is None:
        allowed = _resolved_sources_disclosable(destination, guild, sources, access_levels)
        return decide(allowed, "permission_structure_proven" if allowed else "destination_audience_unavailable")
    humans = {member.id: member for member in viewers
              if member.id != bot_member.id and not getattr(member, "bot", False)}
    for actor in humans.values():
        if not has_access_requirements(guild, actor.id, access_levels):
            return decide(False, "viewer_missing_required_access", member_id=actor.id)
    actors = {**humans, bot_member.id: bot_member}
    for source in sources.values():
        same_or_public = source.id == destination.id or _public_read_source(source, guild)
        private_members = None
        if not same_or_public and isinstance(source, discord.Thread) and source.is_private():
            private_members = await _thread_member_ids(source, cache)
            if private_members is None:
                allowed = _resolved_sources_disclosable(destination, guild, sources, access_levels)
                return decide(allowed, "permission_structure_proven" if allowed else "source_thread_members_unavailable",
                              source_id=source.id)
        for actor in actors.values():
            if same_or_public and actor.id != bot_member.id:
                continue
            permissions = source.permissions_for(actor)
            if not (permissions.view_channel and permissions.read_message_history):
                return decide(False, "viewer_source_read_denied", source_id=source.id, member_id=actor.id)
            if private_members is not None and actor.id not in private_members:
                return decide(False, "viewer_not_source_thread_member", source_id=source.id, member_id=actor.id)
    return decide(True, "all_destination_viewers_authorized")


async def can_show_in_dm(guild, member_id, sources, levels):
    """Check the recipient's current roles and source-channel membership."""
    if not has_access_requirements(guild, member_id, set(levels)):
        return False
    member = guild.get_member(member_id)
    for source in sources.values() if isinstance(sources, Mapping) else sources:
        channel = guild.get_channel_or_thread(source) if type(source) is int else source
        if channel is None:
            return False
        permissions = channel.permissions_for(member)
        if not (permissions.view_channel and permissions.read_message_history):
            return False
        if isinstance(channel, discord.Thread) and channel.is_private():
            try:
                await channel.fetch_member(member_id)
            except discord.HTTPException:
                return False
    return True
