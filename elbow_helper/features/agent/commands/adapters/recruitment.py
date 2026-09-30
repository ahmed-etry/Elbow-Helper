"""Recruitment command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.features.agent.tools.discord_safety import (
    check_member, check_post_access, resolve_channel, resolve_member,
)

from ...actions.contracts import ActionClass, ChangePreview
from ...wording import (
    ACTION_PREVIEW_BLANK, ACTION_RECSTATEMENT_APPLICANT_UNAVAILABLE,
    ACTION_RECSTATEMENT_LABEL, ACTION_RECSTATEMENT_LINE,
    ACTION_RECSTATEMENT_UNAVAILABLE, ACTION_CHECKUP_LABEL,
    ACTION_CHECKUP_LINE,
)
from ..outcomes import CommandOutcome
from ..registry import CommandAdapter, PreparedCommandChange


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


async def prepare_checkup(context: Any,
                          values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Recruitment")
    if workflow is None:
        raise ValueError(ACTION_RECSTATEMENT_UNAVAILABLE)
    member = await resolve_member(context.guild, values["applicant"])
    check_member(member, context.guild.me)
    channel = await resolve_channel(
        context, values.get("channel") or context.source_message.channel.id,
    )
    check_post_access(channel, context.member, context.guild.me)
    prepared = workflow.prepare_checkup(
        member, values["account_linked"], channel,
        values.get("additional_notes"),
    )
    if prepared["issue"]:
        raise ValueError(prepared["issue"])

    async def recheck() -> bool:
        try:
            current_member = await resolve_member(context.guild, member.id, fresh=True)
            check_member(current_member, context.guild.me)
            check_post_access(channel, context.member, context.guild.me)
        except (ValueError, discord.DiscordException):
            return False
        current = workflow.prepare_checkup(
            current_member, values["account_linked"], channel,
            values.get("additional_notes"),
        )
        return (current["issue"] is None
                and current["message"] == prepared["message"])

    async def run() -> CommandOutcome:
        confirmation = await workflow.post_checkup(prepared)
        return CommandOutcome(
            "complete", "private", text=confirmation,
            result={"channel_id": channel.id},
        )

    return PreparedCommandChange(ChangePreview((
        ACTION_CHECKUP_LINE.format(channel=channel.mention),
        *(line or ACTION_PREVIEW_BLANK
          for line in str(prepared["message"]).splitlines()),
    ), recheck, summary=ACTION_CHECKUP_LABEL), run)


async def run_checkup(context: Any,
                      values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_checkup(context, values)).run()


def recruitment_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter("/opinion", "private", run_opinion,
                       entity_options=(("ticket", "discord_channel"),)),
        CommandAdapter("/recstatements", "confirm", run_recstatement,
                       prepare=prepare_recstatement,
                       action_class=ActionClass.IRREVERSIBLE),
        CommandAdapter("/checkup", "confirm", run_checkup,
                       prepare=prepare_checkup,
                       action_class=ActionClass.IRREVERSIBLE,
                       entity_options=(("applicant", "discord_member"),
                                       ("channel", "discord_channel"))),
    )
