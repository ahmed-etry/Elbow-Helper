"""Synthetic Discord entities for permission-checked management workflows."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from elbow_helper.agent.models import AgentTurnState


class Entity(SimpleNamespace):
    def __hash__(self):
        return hash(self.id)


def fixture():
    requester = Entity(id=10, display_name="Synthetic requester", roles=[],
                       top_role=Entity(id=50, position=50),
                       guild_permissions=discord.Permissions.all())
    bot = Entity(id=20, top_role=Entity(id=100, position=100),
                 guild_permissions=discord.Permissions.all())
    role = Entity(id=30, position=2, mention="@Synthetic role", managed=False,
                  permissions=discord.Permissions.none(), is_default=lambda: False)
    member = Entity(id=40, mention="@Synthetic member", display_name="Synthetic member",
                    top_role=role, roles=[role], guild_permissions=discord.Permissions.none())
    actors = {item.id: item for item in (requester, bot, member)}
    channel = Entity(id=200, mention="#synthetic", name="synthetic", overwrites={}, category=None,
                     permissions_for=lambda actor: actor.guild_permissions)
    channels = {channel.id: channel}
    guild = Entity(id=1, owner_id=999, me=bot, filesize_limit=8 * 1024 * 1024,
                   get_member=actors.get, fetch_member=AsyncMock(side_effect=actors.get),
                   get_role=lambda identifier: role if identifier == role.id else None,
                   get_channel_or_thread=channels.get)
    channel.guild = guild

    async def set_permissions(target, *, overwrite, reason):
        if overwrite is None:
            channel.overwrites.pop(target, None)
        else:
            channel.overwrites[target] = overwrite

    channel.set_permissions = AsyncMock(side_effect=set_permissions)
    return Entity(guild=guild, member=requester, bot=Entity(), state=AgentTurnState(),
                  source_message=Entity(id=300, channel=channel)), channel, role, member
