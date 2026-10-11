"""Resolve feature targets and check permissions for raw Discord changes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from ..wording import ACTION_ROLE_HIBERNATING, ACTION_ROLE_OPEN_TRIAL, ACTION_UNAVAILABLE

from elbow_helper.configuration.clans import CLAN_MEMBER_ROLE_IDS
from elbow_helper.configuration.roles import ALLIANCE_MEMBER_ROLE_ID, MEMBER_ROLE_ID
from elbow_helper.features.hibernation.config import (
    HIBERNATE_REMOVE_ROLE_IDS, RESTORE_ROLE_IDS, SNAPSHOT_ONLY_ROLE_IDS,
    managed_role_ids as hibernation_roles,
)
from elbow_helper.features.recruitment.config import managed_role_ids as recruitment_roles
from elbow_helper.features.wars.roles import managed_role_ids as war_roles
from ..actions.contracts import ActionRefused


POWERFUL_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "manage_webhooks", "manage_messages", "kick_members", "ban_members",
    "moderate_members", "mention_everyone",
)


class DiscordActionRefused(ActionRefused):
    """A Discord target is outside the agent's action boundary."""


def member_role_owner(context: Any, role_id: int, member_id: int) -> str | None:
    """Roles saved or changed by a flow belong to it while the member is in it."""
    membership_roles = {
        MEMBER_ROLE_ID, ALLIANCE_MEMBER_ROLE_ID, *CLAN_MEMBER_ROLE_IDS.values(),
    }
    if role_id in membership_roles:
        if context.recruitment_queries is None:
            raise DiscordActionRefused(ACTION_UNAVAILABLE)
        if context.recruitment_queries.has_open_trial(member_id):
            return "trial"
    saved_roles = {
        *membership_roles, *HIBERNATE_REMOVE_ROLE_IDS,
        *RESTORE_ROLE_IDS, *SNAPSHOT_ONLY_ROLE_IDS,
    }
    if role_id in saved_roles:
        if context.hibernation_queries is None:
            raise DiscordActionRefused(ACTION_UNAVAILABLE)
        if context.hibernation_queries.is_hibernating(member_id):
            return "hibernation"
    return None


def check_member_role_ownership(context: Any, role: Any, member: Any) -> None:
    owner = member_role_owner(context, role.id, member.id)
    if owner is not None:
        template = ACTION_ROLE_OPEN_TRIAL if owner == "trial" else ACTION_ROLE_HIBERNATING
        raise DiscordActionRefused(template.format(member=member.display_name))


async def managed_role_commands(context: Any) -> dict[int, str]:
    owners: dict[int, str] = {}
    for role_id in recruitment_roles():
        owners[role_id] = "/accept"
    for role_id in hibernation_roles():
        owners.setdefault(role_id, "/hibernate")
    for role_id in war_roles():
        owners.setdefault(role_id, "/roster")
    if context.roster_queries is not None:
        for role_id in await context.roster_queries.managed_role_ids(context.guild.id):
            owners[role_id] = "/roster"
    if context.role_connection_queries is not None:
        for role_id in context.role_connection_queries.managed_role_ids():
            owners[role_id] = "/connections"
    return owners


def check_role(role: Any, guild: Any, bot_member: Any,
               owners: Mapping[int, str], *, requester: Any) -> None:
    if role is None:
        raise DiscordActionRefused("That role is unavailable.")
    if role.id == guild.id or role.is_default():
        raise DiscordActionRefused("The everyone role cannot be changed.")


def check_member(member: Any, bot_member: Any, *, guild: Any, requester: Any) -> None:
    if member is None:
        raise DiscordActionRefused("That member is unavailable.")
    if bot_member is None or member.id == bot_member.id:
        raise DiscordActionRefused("The bot cannot change its own membership.")


def _below_requester(position: int, guild: Any, requester: Any) -> bool:
    current = guild.get_member(requester.id)
    if current is None or current.id != requester.id:
        return False
    if current.id == getattr(guild, "owner_id", None):
        return True
    return position < current.top_role.position


async def resolve_member(guild: Any, member_id: int, *, fresh: bool = False) -> Any:
    if not fresh:
        member = guild.get_member(member_id)
        if member is not None:
            return member
    try:
        return await guild.fetch_member(member_id)
    except discord.NotFound as error:
        raise DiscordActionRefused("That member is unavailable.") from error


async def resolve_channel(context: Any, channel_id: int) -> Any:
    channel = context.guild.get_channel_or_thread(channel_id)
    if channel is None:
        try:
            channel = await context.bot.fetch_channel(channel_id)
        except discord.NotFound as error:
            raise DiscordActionRefused("That channel is unavailable.") from error
    if getattr(getattr(channel, "guild", None), "id", None) != context.guild.id:
        raise DiscordActionRefused("That channel is unavailable.")
    return channel


def check_post_access(channel: Any, member: Any, bot_member: Any) -> None:
    check_view_access(channel, member, bot_member)
    permission = "send_messages_in_threads" if isinstance(channel, discord.Thread) else "send_messages"
    for actor in (member, bot_member):
        allowed = channel.permissions_for(actor)
        if not getattr(allowed, permission, False):
            raise DiscordActionRefused("Both you and the bot need access to post there.")


def check_view_access(channel: Any, member: Any, bot_member: Any) -> None:
    for actor in (member, bot_member):
        if actor is None or not channel.permissions_for(actor).view_channel:
            raise DiscordActionRefused("Both you and the bot need access to view that channel.")


def check_raw_role(role: Any, guild: Any, bot_member: Any,
                   owners: Mapping[int, str], *, requester: Any) -> None:
    check_role(role, guild, bot_member, owners, requester=requester)
    if role.managed:
        raise DiscordActionRefused("An integration manages that role.")
    current = guild.get_member(requester.id)
    if current is None or not getattr(current.guild_permissions, "manage_roles", False):
        raise DiscordActionRefused(ACTION_UNAVAILABLE)
    if not _below_requester(role.position, guild, requester):
        raise DiscordActionRefused("That role is above your top role.")
    for permission in POWERFUL_PERMISSIONS:
        if getattr(role.permissions, permission, False):
            raise DiscordActionRefused("That role grants server management access.")
    owner = owners.get(role.id)
    if owner:
        raise DiscordActionRefused(f"{owner} manages that role.")


def check_raw_nickname(member: Any, bot_member: Any, *, guild: Any, requester: Any) -> None:
    check_member(member, bot_member, guild=guild, requester=requester)
    current = guild.get_member(requester.id)
    if current is None or not getattr(current.guild_permissions, "manage_nicknames", False):
        raise DiscordActionRefused(ACTION_UNAVAILABLE)
    if (member.id != requester.id
            and (member.id == getattr(guild, "owner_id", None)
                 or not _below_requester(member.top_role.position, guild, requester))):
        raise DiscordActionRefused("That member ranks at or above you.")
