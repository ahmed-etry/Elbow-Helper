"""Synthetic contexts for exercising agent lookups and delivery."""

from datetime import datetime, timezone
from types import SimpleNamespace

import discord

from elbow_helper.agent.engine.models import AgentRequestContext
from elbow_helper.configuration.roles import CORE


def member_context():
    member = SimpleNamespace(id=42, display_name="Ahmad", roles=[SimpleNamespace(id=next(iter(CORE)))])
    channel = SimpleNamespace(
        id=100, type=discord.ChannelType.text, overwrites={},
        permissions_for=lambda member: SimpleNamespace(view_channel=True, read_message_history=True),
    )
    message = SimpleNamespace(
        channel=channel,
        created_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
    )
    default_role = SimpleNamespace(id=1)
    guild = SimpleNamespace(
        id=1, name="Brown Elbow", me=SimpleNamespace(id=999, bot=True, roles=[]), chunked=True, members=[member],
        default_role=default_role, roles=[default_role, *member.roles],
        get_member=lambda member_id: member,
    )
    channel.guild = guild
    guild.get_channel_or_thread = lambda channel_id: channel if channel_id == 100 else None
    return AgentRequestContext(
        bot=None, account_links=None, message_search=None,
        member=member,
        source_message=message,
        guild=guild,
    )


def flow_context():
    actor = SimpleNamespace(id=41, display_name="Test member", bot=False,
                            roles=[SimpleNamespace(id=next(iter(CORE)))])
    channel = SimpleNamespace(
        id=91, type=discord.ChannelType.text, overwrites={},
        permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True),
    )
    role = SimpleNamespace(id=1)
    guild = SimpleNamespace(
        id=3, name="Test guild", me=SimpleNamespace(id=999, bot=True, roles=[]), chunked=True, members=[actor], roles=[role, *actor.roles],
        default_role=role, get_member=lambda _: actor,
        get_channel_or_thread=lambda _: channel,
        channels=(channel,),
    )
    channel.guild = guild
    message = SimpleNamespace(id=71, channel=channel,
                              created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    return AgentRequestContext(
        bot=None, guild=guild, member=actor, source_message=message,
        account_links=None, message_search=None,
    )
