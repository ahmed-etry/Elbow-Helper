"""Recruitment command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.features.agent.tools.discord_safety import (
    check_post_access, resolve_channel,
)

from ...actions.contracts import ActionClass, ChangePreview
from ...wording import (
    ACTION_PREVIEW_BLANK, ACTION_RECSTATEMENT_APPLICANT_UNAVAILABLE,
    ACTION_RECSTATEMENT_LABEL, ACTION_RECSTATEMENT_LINE,
    ACTION_RECSTATEMENT_UNAVAILABLE,
)
from ..outcomes import CommandOutcome
from ..registry import CommandAdapter


async def run_opinion(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    ticket = str(values.get("ticket") or "").strip()
    if not ticket:
        return CommandOutcome.needs_input(("ticket",))
    workflow = context.bot.get_cog("Recruitment")
    if workflow is None:
        return CommandOutcome.unavailable()
    channel = workflow.resolve_opinion_ticket(context.guild, context.member, ticket)
    if channel is None:
        return CommandOutcome.needs_input(("ticket",))
    parts = await workflow.build_ticket_second_opinion(channel)
    if not parts:
        return CommandOutcome("empty", "private")
    return CommandOutcome("complete", "private", private_parts=tuple(parts))


async def _recstatement(context: Any, values: Mapping[str, Any]):
    workflow = context.bot.get_cog("Recruitment")
    if workflow is None:
        raise ValueError(ACTION_RECSTATEMENT_UNAVAILABLE)
    member = context.guild.get_member(values["applicant"])
    if member is None:
        try:
            member = await context.guild.fetch_member(values["applicant"])
        except discord.DiscordException:
            raise ValueError(ACTION_RECSTATEMENT_APPLICANT_UNAVAILABLE) from None
    channel_id = values.get("channel") or context.source_message.channel.id
    channel = await resolve_channel(context, channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError("Run this command in a server text channel.")
    check_post_access(channel, context.member, context.guild.me)
    prepared = workflow.prepare_recstatement(
        values["message"], member, channel, values.get("additional_notes"),
    )
    if prepared["issue"]:
        raise ValueError(prepared["issue"])
    return workflow, prepared


async def prepare_recstatement(context: Any,
                               values: Mapping[str, Any]) -> ChangePreview:
    workflow, prepared = await _recstatement(context, values)

    async def recheck() -> bool:
        try:
            current, live = await _recstatement(context, values)
        except ValueError:
            return False
        return (current is workflow
                and live["channel"].id == prepared["channel"].id
                and live["message"] == prepared["message"])

    return ChangePreview((
        ACTION_RECSTATEMENT_LINE.format(channel=prepared["channel"].mention),
        *(line or ACTION_PREVIEW_BLANK
          for line in prepared["message"].splitlines()),
    ), recheck, summary=ACTION_RECSTATEMENT_LABEL)


async def run_recstatement(context: Any,
                           values: Mapping[str, Any]) -> CommandOutcome:
    workflow, prepared = await _recstatement(context, values)
    message = await workflow.post_recstatement(prepared)
    return CommandOutcome("complete", "private", text=message,
                          result={"channel_id": prepared["channel"].id})


def recruitment_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter("/opinion", "private", run_opinion,
                       entity_options=(("ticket", "discord_channel"),)),
        CommandAdapter("/recstatements", "confirm", run_recstatement,
                       prepare=prepare_recstatement,
                       action_class=ActionClass.IRREVERSIBLE),
    )
