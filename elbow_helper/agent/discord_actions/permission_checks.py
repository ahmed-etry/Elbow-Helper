"""Permission and hierarchy facts shared by confirmed Discord management actions."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import discord


def missing_permissions(
    actor: Any, names: Iterable[str], *, channel: Any = None,
) -> tuple[str, ...]:
    """Use the permissions in the affected place, rather than the request's channel."""
    if actor is None:
        return tuple(names)
    allowed = channel.permissions_for(actor) if channel is not None else actor.guild_permissions
    return tuple(name for name in names if not getattr(allowed, name, False))


def below_both_roles(role: Any, requester: Any, bot_member: Any) -> bool:
    """A target must rank below both actors; equal roles never authorize a change."""
    if role is None or requester is None or bot_member is None:
        return False
    if isinstance(role, discord.Role):
        return role < requester.top_role and role < bot_member.top_role
    return role.position < min(requester.top_role.position, bot_member.top_role.position)


def unheld_permissions(granted: discord.Permissions, actor: Any,
                       *, channel: Any = None) -> tuple[str, ...]:
    """Return every enabled permission the actor cannot grant in the affected place."""
    names = tuple(name for name, enabled in granted if enabled)
    return missing_permissions(actor, names, channel=channel)
