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
    allowed = (channel.permissions_for(actor) if channel is not None
               else getattr(actor, "guild_permissions", None))
    return tuple(name for name in names if not getattr(allowed, name, False))


# What the direct Discord actions require of the asker: in the channel they are asked from, and server-wide.
CHANNEL_ACTION_PERMISSIONS = (
    "read_message_history", "send_messages", "add_reactions", "attach_files",
    "send_polls", "mention_everyone", "manage_messages", "manage_threads",
    "create_public_threads", "create_private_threads",
)
SERVER_ACTION_PERMISSIONS = ("manage_channels", "manage_roles", "manage_nicknames", "moderate_members")


def asker_permissions(member: Any, channel: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The action permissions the asker holds and lacks, by the same check the actions run."""
    sending = "send_messages_in_threads" if isinstance(channel, discord.Thread) else "send_messages"
    channel_names = tuple(sending if name == "send_messages" else name for name in CHANNEL_ACTION_PERMISSIONS)
    lacking = {*missing_permissions(member, channel_names, channel=channel),
               *missing_permissions(member, SERVER_ACTION_PERMISSIONS)}
    names = (*channel_names, *SERVER_ACTION_PERMISSIONS)
    return (tuple(name for name in names if name not in lacking),
            tuple(name for name in names if name in lacking))


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
