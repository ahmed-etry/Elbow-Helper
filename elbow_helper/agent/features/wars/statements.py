"""War statement commands through their feature's prepared message."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.agent.discord_actions.safety import check_bot_post_access, find_member
from elbow_helper.features.wars.commands import resolve_statement_members

from elbow_helper.agent.actions.contracts import ActionRefused, ActionClass, ChangePreview
from elbow_helper.agent.conversation.wording import ACTION_WAR_STATEMENT_LABEL, ACTION_WAR_STATEMENT_LINE
from elbow_helper.agent.actions.outcomes import ActionOutcome
from elbow_helper.agent.commands.registry import CommandAdapter


async def _statement(context: Any, values: Mapping[str, Any], kind: str):
    workflow = context.bot.get_cog("WarStatements")
    if workflow is None:
        raise ActionRefused("War statements are unavailable")
    if kind == "first_claim":
        victim = await find_member(context.guild, values["victim"])
        attacker = await find_member(context.guild, values["attacker"])
        if victim is None or attacker is None:
            raise ActionRefused('That player is unavailable.')
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
        raise ActionRefused(prepared["issue"])
    check_bot_post_access(prepared["post_channel"], context.guild.me)
    return workflow, prepared


def _adapter(path: str, kind: str, tool_description: str | None = None) -> CommandAdapter:
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
        ), recheck, summary=ACTION_WAR_STATEMENT_LABEL,
           details=tuple(message.split("\n")), message_body=slice(None))

    async def run(context: Any, values: Mapping[str, Any]) -> ActionOutcome:
        workflow, statement = await _statement(context, values, kind)
        ok, _ = await workflow.post_statement(
            statement["post_channel"], statement["message"],
        )
        if not ok:
            return ActionOutcome.unavailable()
        return ActionOutcome(
            "complete", "private", text=f"War statement posted in {statement['post_channel'].mention}.",
            result={"channel_id": statement["post_channel"].id},
        )

    return CommandAdapter(path, "confirm", run, prepare=prepare,
                          action_class=ActionClass.CHANGE, tool_description=tool_description)


def war_statement_adapters() -> tuple[CommandAdapter, ...]:
    return (
        _adapter("/warstatement first-claim", "first_claim"),
        _adapter("/warstatement breaking-rules", "breaking_rules"),
        _adapter(
            "/warstatement one-attack-missed", "one_attack_missed",
            "Carry out /warstatement one-attack-missed for an authorized requester to ask players why they used only one attack.",
        ),
        _adapter("/warstatement war-filler", "war_filler"),
        _adapter(
            "/warstatement missed-attacks", "missed_attacks",
            "Carry out /warstatement missed-attacks for an authorized requester to notify players removed from war after missing both attacks.",
        ),
    )
