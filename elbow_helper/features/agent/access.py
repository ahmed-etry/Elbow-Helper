"""Current Core membership and source-channel access for agent requests."""

import discord

from elbow_helper.configuration.roles import CORE


class AgentAccessLost(RuntimeError):
    """The requester can no longer use this agent conversation."""


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
