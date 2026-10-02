"""Source audiences and retained-evidence disclosure."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.configuration.roles import LEAD, LEAD_PLUS

from .access import (
    ACCESS_LEAD, ACCESS_LEAD_PLUS, KNOWN_ACCESS_REQUIREMENTS,
    AgentAccessLost, accessible_message_channel, has_access_requirements,
    require_evidence_access,
)
from .models import AgentRequestContext


class AgentDisclosureDenied(AgentAccessLost):
    """Authorized source evidence cannot be sent to this channel audience."""


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
    if not requirements <= KNOWN_ACCESS_REQUIREMENTS:
        return False
    destination = channel.parent if isinstance(channel, discord.Thread) else channel
    roles = getattr(guild, "roles", None)
    default_role = getattr(guild, "default_role", None)
    signature = _read_audience_signature(destination)
    if roles is None or default_role is None or signature is None:
        return False
    allowed_ids = set(LEAD if ACCESS_LEAD in requirements else LEAD_PLUS)
    if ACCESS_LEAD_PLUS in requirements:
        allowed_ids &= LEAD_PLUS
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
    overwrites = destination.overwrites
    if any(
        getattr(overwrite, "view_channel", None) is True
        and getattr(target, "id", None) not in known_roles
        for target, overwrite in overwrites.items()
    ):
        return False
    return True


async def can_disclose_provenance(
    context: AgentRequestContext, source_channels: frozenset[int] | set[int],
    required_access: frozenset[str] | set[str],
) -> bool:
    """Prove that every current destination viewer may see all source evidence."""
    destination = getattr(getattr(context, "source_message", None), "channel", None)
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
    return _resolved_sources_disclosable(
        destination, context.guild, sources, required_access,
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


def require_destination_access(
    context: AgentRequestContext, sources: Mapping[int, Any],
) -> None:
    if not _resolved_sources_disclosable(
        context.source_message.channel, context.guild, sources,
        context.state.required_access,
    ):
        raise AgentDisclosureDenied("Evidence cannot be shared in this channel")


async def require_disclosure_access(context: AgentRequestContext) -> None:
    sources = await require_evidence_access(context)
    require_destination_access(context, sources)
