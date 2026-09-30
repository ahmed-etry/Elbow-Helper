"""War statement commands through their feature's prepared message."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.features.agent.tools.discord_safety import (
    check_post_access, check_view_access,
)
from elbow_helper.features.wars.commands import resolve_statement_members

from ...actions.contracts import ActionClass, ChangePreview
from ...wording import (
    ACTION_PREVIEW_BLANK, ACTION_WAR_STATEMENT_LABEL,
    ACTION_WAR_STATEMENT_LINE, ACTION_WAR_STATEMENT_PLAYER_UNAVAILABLE,
)
from ..outcomes import CommandOutcome
from ..registry import CommandAdapter


async def _member(guild: Any, member_id: int):
    member = guild.get_member(member_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(member_id)
    except discord.DiscordException:
        return None


async def _statement(context: Any, values: Mapping[str, Any], kind: str):
    workflow = context.bot.get_cog("WarStatements")
    if workflow is None:
        raise ValueError("War statements are unavailable")
    if kind == "first_claim":
        victim = await _member(context.guild, values["victim"])
        attacker = await _member(context.guild, values["attacker"])
        if victim is None or attacker is None:
            raise ValueError(ACTION_WAR_STATEMENT_PLAYER_UNAVAILABLE)
        prepared = workflow.prepare_statement(
            kind, context.guild, values["clan"],
            victim=victim, attacker=attacker, notes=values.get("notes"),
        )
    else:
        players = await resolve_statement_members(
            context.guild, values["players"],
        )
        prepared = workflow.prepare_statement(
            kind, context.guild, values["clan"],
            players=players, notes=values.get("notes"),
        )
    if prepared["issue"]:
        raise ValueError(prepared["issue"])
    check_post_access(prepared["post_channel"], context.member, context.guild.me)
    check_view_access(prepared["clan_war_channel"], context.member, context.guild.me)
    return workflow, prepared


def _adapter(path: str, kind: str) -> CommandAdapter:
    async def prepare(context: Any, values: Mapping[str, Any]) -> ChangePreview:
        workflow, statement = await _statement(context, values, kind)
        post_channel = statement["post_channel"]
        message = statement["message"]

        async def recheck() -> bool:
            try:
                current, live = await _statement(context, values, kind)
            except ValueError:
                return False
            return (current is workflow
                    and live["post_channel"].id == post_channel.id
                    and live["message"] == message)

        return ChangePreview((
            ACTION_WAR_STATEMENT_LINE.format(channel=post_channel.mention),
            *(line or ACTION_PREVIEW_BLANK for line in message.splitlines()),
        ), recheck, summary=ACTION_WAR_STATEMENT_LABEL)

    async def run(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
        workflow, statement = await _statement(context, values, kind)
        ok, result = await workflow.post_statement(
            statement["post_channel"], statement["message"],
        )
        if not ok:
            return CommandOutcome.unavailable()
        return CommandOutcome(
            "complete", "private", text=result,
            result={"channel_id": statement["post_channel"].id},
        )

    return CommandAdapter(path, "confirm", run, prepare=prepare,
                          action_class=ActionClass.IRREVERSIBLE)


def war_statement_adapters() -> tuple[CommandAdapter, ...]:
    return (
        _adapter("/warstatement first-claim", "first_claim"),
        _adapter("/warstatement breaking-rules", "breaking_rules"),
        _adapter("/warstatement one-attack-missed", "one_attack_missed"),
        _adapter("/warstatement war-filler", "war_filler"),
        _adapter("/warstatement missed-attacks", "missed_attacks"),
    )
