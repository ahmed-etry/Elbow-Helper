"""Recruitment command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.features.agent.tools.discord_safety import (
    check_member, check_post_access, check_role, resolve_channel, resolve_member,
)

from ...actions.contracts import ActionClass, ChangePreview
from ...wording import (
    ACTION_PREVIEW_BLANK, ACTION_RECSTATEMENT_APPLICANT_UNAVAILABLE,
    ACTION_RECSTATEMENT_LABEL, ACTION_RECSTATEMENT_LINE,
    ACTION_RECSTATEMENT_UNAVAILABLE, ACTION_CHECKUP_LABEL,
    ACTION_CHECKUP_LINE,
    ACTION_DECLINE_LABEL, ACTION_DECLINE_LINE, ACTION_DECLINE_RENAME,
    ACTION_DECLINE_RENAME_SKIP,
    ACTION_FINALIZE_LABEL, ACTION_FINALIZE_LINE,
    ACTION_FINALIZE_RENAME, ACTION_FINALIZE_ROLE_ADD,
    ACTION_FINALIZE_ROLE_REMOVE,
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


async def prepare_decline(context: Any,
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
    prepared = workflow.prepare_decline(
        member, channel, values.get("additional_notes"),
    )
    if prepared["issue"]:
        raise ValueError(prepared["issue"])
    candidate = prepared["rename_candidate"]
    lines = [ACTION_DECLINE_LINE.format(member=member.mention,
                                        channel=channel.mention)]
    if candidate and candidate != channel.name:
        lines.append((ACTION_DECLINE_RENAME if len(candidate) <= 100
                      else ACTION_DECLINE_RENAME_SKIP).format(
            old=channel.name, new=candidate,
        ))
    lines.extend(line or ACTION_PREVIEW_BLANK
                 for line in prepared["message"].splitlines())

    async def recheck() -> bool:
        try:
            live_member = await resolve_member(context.guild, member.id, fresh=True)
            check_member(live_member, context.guild.me)
            check_post_access(channel, context.member, context.guild.me)
        except (ValueError, discord.DiscordException):
            return False
        live = workflow.prepare_decline(
            live_member, channel, values.get("additional_notes"),
        )
        return (live["issue"] is None
                and live["message"] == prepared["message"]
                and live["rename_candidate"] == candidate)

    async def run() -> CommandOutcome:
        message = await workflow.post_decline(prepared)
        return CommandOutcome("complete", "private", text=message,
                              result={"channel_id": channel.id},
                              after={"channel_id": channel.id,
                                     "channel_name": channel.name})

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_DECLINE_LABEL), run,
    )


async def run_decline(context: Any,
                      values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_decline(context, values)).run()


async def prepare_finalize(context: Any,
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
    prepared = workflow.prepare_finalize(
        member, channel, context.guild, values.get("additional_notes"),
    )
    if prepared["issue"]:
        raise ValueError(prepared["issue"])

    def check_roles() -> None:
        for role in (*prepared["remove_roles"], *prepared["add_roles"]):
            check_role(role, context.guild, context.guild.me, {})

    check_roles()
    lines = [ACTION_FINALIZE_LINE.format(member=member.mention,
                                         channel=channel.mention)]
    if prepared["old_name"] != prepared["new_name"]:
        lines.append(ACTION_FINALIZE_RENAME.format(
            old=prepared["old_name"], new=prepared["new_name"],
        ))
    lines.extend(ACTION_FINALIZE_ROLE_REMOVE.format(
        role=role.mention, member=member.mention,
    ) for role in prepared["remove_roles"])
    lines.extend(ACTION_FINALIZE_ROLE_ADD.format(
        role=role.mention, member=member.mention,
    ) for role in prepared["add_roles"])
    lines.extend(line or ACTION_PREVIEW_BLANK
                 for line in prepared["message"].splitlines())
    signature = (
        prepared["old_name"], prepared["new_name"], prepared["message"],
        tuple(role.id for role in prepared["remove_roles"]),
        tuple(role.id for role in prepared["add_roles"]),
    )

    async def recheck() -> bool:
        try:
            live_member = await resolve_member(context.guild, member.id, fresh=True)
            check_member(live_member, context.guild.me)
            check_post_access(channel, context.member, context.guild.me)
            check_roles()
        except (ValueError, discord.DiscordException):
            return False
        live = workflow.prepare_finalize(
            live_member, channel, context.guild,
            values.get("additional_notes"),
        )
        if live["issue"]:
            return False
        return signature == (
            live["old_name"], live["new_name"], live["message"],
            tuple(role.id for role in live["remove_roles"]),
            tuple(role.id for role in live["add_roles"]),
        )

    async def run() -> CommandOutcome:
        confirmation = await workflow.post_finalize(prepared)
        return CommandOutcome(
            "complete", "private", text=confirmation,
            result={"channel_id": channel.id, "member_id": member.id},
            after={"channel_id": channel.id, "member_id": member.id,
                   "channel_name": channel.name,
                   "role_ids": tuple(role.id for role in member.roles)},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_FINALIZE_LABEL), run,
    )


async def run_finalize(context: Any,
                       values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_finalize(context, values)).run()


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
        CommandAdapter("/decline", "confirm", run_decline,
                       prepare=prepare_decline,
                       action_class=ActionClass.IRREVERSIBLE,
                       entity_options=(("applicant", "discord_member"),
                                       ("channel", "discord_channel"))),
        CommandAdapter("/finalize", "confirm", run_finalize,
                       prepare=prepare_finalize,
                       action_class=ActionClass.IRREVERSIBLE,
                       entity_options=(("applicant", "discord_member"),
                                       ("channel", "discord_channel"))),
    )
