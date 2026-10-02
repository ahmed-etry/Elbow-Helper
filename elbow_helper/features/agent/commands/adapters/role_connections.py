"""Role connections command adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.features.agent.access import ACCESS_LEAD, has_access_requirements
from elbow_helper.features.agent.tools.discord_safety import (
    check_post_access, resolve_channel,
)
from elbow_helper.features.help.discovery import ParameterInfo

from ...actions.contracts import ChangePreview
from ...wording import ACTION_CONNECTIONS_BOARD_LABEL, ACTION_CONNECTIONS_BOARD_LINE
from ...actions.outcomes import CommandOutcome, embed_text
from ..registry import CommandAdapter


async def _target(context: Any, values: Mapping[str, Any]) -> tuple[Any, Any]:
    if not has_access_requirements(context.guild, context.member.id, {ACCESS_LEAD}):
        raise ValueError("Role connections are unavailable")
    workflow = context.bot.get_cog("RoleConnections")
    if workflow is None:
        raise ValueError("Role connections are unavailable")
    context.state.required_access.add(ACCESS_LEAD)
    channel_id = values.get("channel") or context.source_message.channel.id
    channel = await resolve_channel(context, channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError("Use a server text channel")
    check_post_access(channel, context.member, context.guild.me)
    return workflow, channel


async def prepare_connections(context: Any, values: Mapping[str, Any]) -> ChangePreview:
    workflow, channel = await _target(context, values)
    signature = workflow.connections_board_signature()
    board = embed_text(workflow.build_connections_embed())

    async def recheck() -> bool:
        try:
            current, target = await _target(context, values)
        except ValueError:
            return False
        return (target.id == channel.id and
                current.connections_board_signature() == signature)

    return ChangePreview((
        ACTION_CONNECTIONS_BOARD_LINE.format(channel=channel.mention),
        *board.splitlines(),
    ), recheck, summary=ACTION_CONNECTIONS_BOARD_LABEL)


async def run_connections(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    workflow, channel = await _target(context, values)
    message = await workflow.post_connections_message(channel)
    return CommandOutcome(
        "complete", result={"message_id": message.id, "channel_id": channel.id},
        after={"message_id": message.id},
    )


def role_connection_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter(
        "/connections", "confirm", run_connections,
        options=(ParameterInfo(
            "channel", "Channel for the role connections board; defaults to this channel.",
            False, "channel",
        ),),
        prepare=prepare_connections,
    ),)
