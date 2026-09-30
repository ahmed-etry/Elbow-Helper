"""Support ticket commands through their public feature operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.features.agent.tools.discord_safety import check_member

from ...actions.contracts import ActionClass, ChangePreview
from ...wording import (
    ACTION_SUPPORT_MEMBER_UNAVAILABLE, ACTION_SUPPORT_OPEN_MEMBER_ACCESS,
    ACTION_SUPPORT_OPEN_BOT_ACCESS, ACTION_SUPPORT_OPEN_ROLE_ACCESS,
    ACTION_SUPPORT_OPEN_DEFAULT_DENY, ACTION_SUPPORT_OPEN_NO_CATEGORY,
    ACTION_SUPPORT_OPEN_DEFAULT_TOPIC, ACTION_SUPPORT_UNAVAILABLE,
    ACTION_SUPPORT_OPEN_CATEGORY, ACTION_SUPPORT_OPEN_CONTROLS,
    ACTION_SUPPORT_OPEN_LABEL, ACTION_SUPPORT_OPEN_LINE,
    ACTION_SUPPORT_OPEN_POST, ACTION_SUPPORT_OPEN_TOPIC,
)
from ..outcomes import CommandOutcome, embed_text
from ..registry import CommandAdapter, PreparedCommandChange


def _workflow(context: Any):
    workflow = context.bot.get_cog("SupportActions")
    if workflow is None:
        raise ValueError(ACTION_SUPPORT_UNAVAILABLE)
    return workflow


async def _member(context: Any, member_id: int):
    member = context.guild.get_member(member_id)
    if member is None:
        try:
            member = await context.guild.fetch_member(member_id)
        except discord.DiscordException:
            raise ValueError(ACTION_SUPPORT_MEMBER_UNAVAILABLE) from None
    check_member(member, context.guild.me)
    return member


def _target_signature(prepared: Mapping[str, Any]):
    category = prepared["category"]
    return (prepared["user"].id, prepared["name"],
            category.id if category else None,
            tuple(sorted(prepared["visible_roles"])),
            prepared["bot_member_id"], prepared["topic"])


async def prepare_support_open(context: Any,
                               values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = _workflow(context)
    member = await _member(context, values["user"])
    prepared = await workflow.prepare_support_ticket(
        context.guild, member, values["topic"], context.member,
    )
    category = prepared["category"]
    lines = [
        ACTION_SUPPORT_OPEN_LINE.format(name=prepared["name"], member=member.mention),
        ACTION_SUPPORT_OPEN_TOPIC.format(
            topic=prepared["topic"] or ACTION_SUPPORT_OPEN_DEFAULT_TOPIC,
        ),
        ACTION_SUPPORT_OPEN_CATEGORY.format(
            category=category.mention if category else ACTION_SUPPORT_OPEN_NO_CATEGORY,
        ),
        ACTION_SUPPORT_OPEN_DEFAULT_DENY,
        ACTION_SUPPORT_OPEN_MEMBER_ACCESS.format(member=member.mention),
        ACTION_SUPPORT_OPEN_BOT_ACCESS,
        *(ACTION_SUPPORT_OPEN_ROLE_ACCESS.format(role=f"<@&{role_id}>")
          for role_id in prepared["visible_roles"]),
        ACTION_SUPPORT_OPEN_POST,
        member.mention,
        prepared["welcome"],
        *embed_text(prepared["embed"]).splitlines(),
        ACTION_SUPPORT_OPEN_CONTROLS,
    ]
    signature = _target_signature(prepared)

    async def recheck() -> bool:
        try:
            live_member = await _member(context, member.id)
        except ValueError:
            return False
        live = workflow.support_ticket_target_state(
            context.guild, live_member, values["topic"], context.member,
        )
        return _target_signature(live) == signature

    async def run() -> CommandOutcome:
        channel, message = await workflow.open_support_ticket(prepared)
        registration = workflow.support_ticket_registration(channel.id)
        if registration is None or registration.get("owner") != member.id:
            raise OSError("Support ticket creation could not be verified")
        return CommandOutcome(
            "complete", "private", text=message,
            result={"channel_id": channel.id}, after={"channel_id": channel.id},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_SUPPORT_OPEN_LABEL),
        run,
    )


async def run_support_open(context: Any,
                           values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_support_open(context, values)).run()


def support_ticket_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/open", "confirm", run_support_open,
                           prepare=prepare_support_open,
                           action_class=ActionClass.IRREVERSIBLE),)
