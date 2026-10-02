"""Confirmed refresh of the Missing Elder board."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import discord

from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..actions.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_ELDER_REFRESH_LINE,
    ACTION_ELDER_REFRESH_BOARD,
    ACTION_ELDER_REFRESH_LABEL,
)
from .discord_safety import check_post_access, resolve_channel


def missing_elder_board_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="refresh_missing_elder_board",
        description="Refresh linked accounts and the Missing Elder board for one clan after confirmation.",
        parameters={"type": "object", "properties": {
            "clan_code": {"type": "string", "enum": list(CLAN_LEADERSHIP_CHANNELS)},
        }, "required": ["clan_code"], "additionalProperties": False},
    ), prepare_missing_elder_board, AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE, True),)


async def prepare_missing_elder_board(context: AgentRequestContext,
                                      values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("ClanReporting")
    if workflow is None:
        raise ValueError("That Missing Elder board couldn't be refreshed.")
    location = workflow.missing_elder_board_location(values["clan_code"])
    if location is None:
        raise ValueError("That Missing Elder board couldn't be refreshed.")
    channel = await resolve_channel(context, location["channel_id"])
    check_post_access(channel, context.member, context.guild.me)
    lines = [ACTION_ELDER_REFRESH_LINE.format(clan=values["clan_code"],
                                             channel=channel.mention)]
    if location["message_id"]:
        lines.append(ACTION_ELDER_REFRESH_BOARD.format(message_id=location["message_id"]))

    async def recheck() -> bool:
        try:
            check_post_access(channel, context.member, context.guild.me)
            return workflow.missing_elder_board_location(values["clan_code"]) == location
        except (discord.DiscordException, ValueError, RuntimeError, KeyError, TypeError, OSError):
            return False

    async def run() -> CommandOutcome:
        if not await workflow.refresh_missing_elder_board_from_accounts(values["clan_code"]):
            raise ValueError("That Missing Elder board couldn't be refreshed.")
        return CommandOutcome("complete", "private", text=ACTION_ELDER_REFRESH_LABEL)

    context.state.command_proposals.append(PreparedAction(
        "refresh_missing_elder_board", dict(values),
        ChangePreview(tuple(lines), recheck, summary=ACTION_ELDER_REFRESH_LABEL),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}
