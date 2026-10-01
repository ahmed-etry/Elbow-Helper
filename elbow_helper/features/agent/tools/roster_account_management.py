"""Confirmed roster signup and removal actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..commands.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_ROSTER_ACCOUNT_LINE, ACTION_ROSTER_ACCOUNT_ITEM,
    ACTION_ROSTER_ACCOUNT_ROLE, ACTION_ROSTER_ACCOUNT_POST,
    ACTION_ROSTER_ACCOUNT_LABEL, ACTION_ROSTER_UNAVAILABLE,
)
from .discord_safety import check_member, check_role, resolve_member


def roster_account_management_tools() -> tuple[RegisteredAgentTool, ...]:
    schema = {"type": "object", "properties": {
        "roster_id": {"type": "integer", "minimum": 1},
        "member_id": {"type": "integer", "minimum": 1},
        "accounts": {"type": "array", "items": {"type": "string"},
                     "minItems": 1, "uniqueItems": True},
    }, "required": ["roster_id", "accounts"], "additionalProperties": False}
    return (
        RegisteredAgentTool(AgentToolDefinition(
            name="signup_roster_accounts",
            description="Sign up selected linked Clash accounts for an open roster after confirmation.",
            parameters=schema,
        ), prepare_roster_signup, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True),
        RegisteredAgentTool(AgentToolDefinition(
            name="remove_roster_accounts",
            description="Remove selected Clash accounts from an open roster after confirmation.",
            parameters=schema,
        ), prepare_roster_removal, AgentCapabilityEffect.COMMAND, ActionClass.IRREVERSIBLE, True),
    )


async def prepare_roster_signup(context: AgentRequestContext,
                                values: Mapping[str, Any]) -> Mapping[str, Any]:
    return await _prepare(context, values, mode="signup")


async def prepare_roster_removal(context: AgentRequestContext,
                                 values: Mapping[str, Any]) -> Mapping[str, Any]:
    return await _prepare(context, values, mode="remove")


async def _prepare(context: AgentRequestContext, values: Mapping[str, Any],
                   *, mode: str) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    roster_id = values["roster_id"]
    member_id = values.get("member_id") or context.member.id
    member = await resolve_member(context.guild, member_id)
    check_member(member, context.guild.me)
    state = await workflow.prepare_roster_account_selection(
        roster_id, member_id, mode=mode,
        for_other_member=member_id != context.member.id)
    roster, picker = state
    if roster is None or roster.guild_id != context.guild.id or not picker.accounts:
        return {"status": "needs_input", "issue": picker.message if picker else ACTION_ROSTER_UNAVAILABLE,
                "prepared_count": 0}
    selected, issue = workflow.resolve_roster_account_choices(
        picker.accounts, list(values["accounts"]))
    if issue:
        return {"status": "needs_input", "issue": issue,
                "available_accounts": [{"name": account.player_name, "tag": account.player_tag}
                                       for account in picker.accounts],
                "prepared_count": 0}
    role = context.guild.get_role(roster.role_id) if roster.role_id else None
    if role is not None:
        check_role(role, context.guild, context.guild.me, {})
    selected_accounts = [account for account in picker.accounts
                         if account.player_tag in selected]
    posts = (await workflow.roster_edit_state(roster))["posts"]
    lines = [ACTION_ROSTER_ACCOUNT_LINE.format(
        action="Sign up" if mode == "signup" else "Remove",
        member=member.mention, name=roster.name)]
    lines.extend(ACTION_ROSTER_ACCOUNT_ITEM.format(
        name=account.player_name, tag=account.player_tag)
        for account in selected_accounts)
    if role is not None:
        lines.append(ACTION_ROSTER_ACCOUNT_ROLE.format(role=role.mention))
    lines.extend(ACTION_ROSTER_ACCOUNT_POST.format(
        message_id=message_id, channel=f"<#{channel_id}>")
        for channel_id, message_id in posts)
    snapshots = {account.player_tag: account for account in picker.accounts}

    async def recheck() -> bool:
        current_roster, current = await workflow.prepare_roster_account_selection(
            roster_id, member_id, mode=mode,
            for_other_member=member_id != context.member.id)
        if current_roster != roster or current is None:
            return False
        return all(snapshots[tag] in current.accounts for tag in selected)

    async def run() -> CommandOutcome:
        result = await workflow.change_roster_accounts(
            roster_id, member_id=member_id, player_tags=selected,
            mode=mode, account_snapshots=snapshots,
            bypass_min_townhall=member_id != context.member.id,
        )
        if not result.changed:
            raise ValueError(result.message)
        return CommandOutcome("complete", "private", text=result.message,
                              after={"accounts": selected})

    context.state.command_proposals.append(PreparedAction(
        "signup_roster_accounts" if mode == "signup" else "remove_roster_accounts",
        {"roster_id": roster_id, "member_id": member_id, "accounts": selected},
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_ACCOUNT_LABEL),
        run, action_class=(ActionClass.CHANGE if mode == "signup"
                           else ActionClass.IRREVERSIBLE),
    ))
    return {"status": "confirmation_required"}
