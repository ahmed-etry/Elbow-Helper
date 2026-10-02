"""Confirmed roster signup and removal actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import ACCESS_LEAD_PLUS, require_evidence_access
from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...actions.outcomes import ActionOutcome
from ...models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ...wording import (
    ACTION_ROSTER_ACCOUNT_LINE,
    ACTION_ROSTER_ACCOUNT_ITEM,
    ACTION_ROSTER_ACCOUNT_ROLE,
    ACTION_ROSTER_POST_REFRESH,
    ACTION_ROSTER_ACCOUNT_LABEL,
    ACTION_ROSTER_BULK_LINE,
    ACTION_ROSTER_BULK_ACCOUNT,
    ACTION_ROSTER_BULK_SIGNED,
    ACTION_ROSTER_BULK_LABEL,
    ACTION_ROSTER_REMOVE_LINE,
    ACTION_ROSTER_REMOVE_ROW,
    ACTION_ROSTER_REMOVE_LABEL,
)
from ...discord_actions.safety import (
    check_member, check_post_access, check_role, resolve_channel, resolve_member,
)


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
        ), prepare_roster_signup, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(
                entity_fields=(
                    ("roster_id", "roster"),
                    ("member_id", "discord_member"),
                    ("accounts", "clash_account_set"),
                ),
                time_fields=(),
                source_scope="request_context",
            ),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="remove_roster_accounts",
            description="Remove selected Clash accounts from an open roster after confirmation.",
            parameters=schema,
        ), prepare_roster_removal, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(
                entity_fields=(
                    ("roster_id", "roster"),
                    ("member_id", "discord_member"),
                    ("accounts", "clash_account_set"),
                ),
                time_fields=(),
                source_scope="request_context",
            ),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="bulk_add_roster_accounts",
            description="Add several linked Clash accounts to one open roster from player tags after confirmation.",
            parameters={"type": "object", "properties": {
                "roster_id": {"type": "integer", "minimum": 1},
                "player_tags": {"type": "array", "items": {"type": "string"},
                                "minItems": 1, "uniqueItems": True},
            }, "required": ["roster_id", "player_tags"], "additionalProperties": False},
        ), prepare_bulk_roster_add, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(
                entity_fields=(("roster_id", "roster"), ("player_tags", "clash_account_set")),
                time_fields=(),
                source_scope="request_context",
                required_access=frozenset({ACCESS_LEAD_PLUS}),
            ),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="remove_roster_signup_rows",
            description="Remove any selected current signup rows from a roster after confirmation.",
            parameters={"type": "object", "properties": {
                "roster_id": {"type": "integer", "minimum": 1},
                "accounts": {"type": "array", "items": {"type": "string"},
                             "minItems": 1, "uniqueItems": True},
            }, "required": ["roster_id", "accounts"], "additionalProperties": False},
        ), prepare_roster_row_removal, AgentCapabilityEffect.COMMAND,
            ActionClass.CHANGE,
            contract=CapabilityContract(
                entity_fields=(("roster_id", "roster"), ("accounts", "clash_account_set")),
                time_fields=(),
                source_scope="request_context",
                required_access=frozenset({ACCESS_LEAD_PLUS}),
            ),
        ),
    )


async def prepare_roster_row_removal(context: AgentRequestContext,
                                     values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError('That roster is unavailable.')
    state = await workflow.roster_signed_rows(values["roster_id"])
    if state is None or state["roster"].guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    roster = state["roster"]
    selected = []
    for value in values["accounts"]:
        tag = normalize_player_tag(value)
        matches = [row for row in state["members"]
                   if row.player_tag == tag
                   or row.player_name.casefold() == value.strip().casefold()]
        if len(matches) != 1:
            return {"status": "needs_input",
                    "issue": ("More than one signed-up account has that name. Use a player tag."
                              if matches else "That account isn't signed up for this roster."),
                    "prepared_count": 0}
        if matches[0].player_tag not in {row.player_tag for row in selected}:
            selected.append(matches[0])
    role = context.guild.get_role(roster.role_id) if roster.role_id else None
    if role is not None:
        check_role(role, context.guild, context.guild.me, {})
    owners = {}
    for row in selected:
        owner = context.guild.get_member(row.discord_user_id)
        if owner is None:
            try:
                owner = await context.guild.fetch_member(row.discord_user_id)
            except discord.NotFound:
                owner = None
        if owner is not None:
            check_member(owner, context.guild.me)
        owners[row.discord_user_id] = owner
    channels = [await resolve_channel(context, channel_id)
                for channel_id, _ in state["posts"]]
    for channel in channels:
        check_post_access(channel, context.member, context.guild.me)
    lines = [ACTION_ROSTER_REMOVE_LINE.format(name=roster.name)]
    lines.extend(ACTION_ROSTER_REMOVE_ROW.format(
        name=row.player_name, tag=row.player_tag,
        member=f"<@{row.discord_user_id}>") for row in selected)
    if role is not None:
        lines.append(ACTION_ROSTER_ACCOUNT_ROLE.format(role=role.mention))
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(
        channel=f"<#{channel_id}>")
        for channel_id, message_id in state["posts"])

    async def recheck() -> bool:
        current = await workflow.roster_signed_rows(values["roster_id"])
        if current != state:
            return False
        try:
            if role is not None:
                check_role(role, context.guild, context.guild.me, {})
            for owner in owners.values():
                if owner is not None:
                    check_member(owner, context.guild.me)
            for channel in channels:
                check_post_access(channel, context.member, context.guild.me)
        except (discord.DiscordException, ValueError, RuntimeError, KeyError, TypeError, OSError):
            return False
        return True

    async def run() -> ActionOutcome:
        result = await workflow.remove_roster_signup_rows(
            values["roster_id"], [row.player_tag for row in selected])
        if not result.changed:
            raise ValueError(result.message)
        return ActionOutcome("complete", "private", text=result.message)

    context.state.proposed_changes.append(PreparedAction(
        "remove_roster_signup_rows", {"roster_id": roster.id,
                                      "accounts": [row.player_tag for row in selected]},
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_REMOVE_LABEL,
                      detail_access=frozenset({ACCESS_LEAD_PLUS})),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}


async def prepare_bulk_roster_add(context: AgentRequestContext,
                                  values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError('That roster is unavailable.')
    raw_tags = " ".join(values["player_tags"])
    try:
        state = await workflow.bulk_add_roster_preview(values["roster_id"], raw_tags)
    except ValueError as exc:
        return {"status": "needs_input", "issue": str(exc), "prepared_count": 0}
    roster = state["roster"]
    if roster.guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    if all(row[3] for row in state["accounts"]):
        return {"status": "no_change"}
    role = context.guild.get_role(roster.role_id) if roster.role_id else None
    if role is not None:
        check_role(role, context.guild, context.guild.me, {})
    members = {member_id: await resolve_member(context.guild, member_id)
               for _, _, member_id, _ in state["accounts"]}
    for member in members.values():
        check_member(member, context.guild.me)
    channels = [await resolve_channel(context, channel_id)
                for channel_id, _ in state["posts"]]
    for channel in channels:
        check_post_access(channel, context.member, context.guild.me)
    lines = [ACTION_ROSTER_BULK_LINE.format(name=roster.name)]
    lines.extend(ACTION_ROSTER_BULK_ACCOUNT.format(
        name=name, tag=tag, member=members[member_id].mention,
        status=ACTION_ROSTER_BULK_SIGNED if signed else "")
        for tag, name, member_id, signed in state["accounts"])
    if role is not None:
        lines.append(ACTION_ROSTER_ACCOUNT_ROLE.format(role=role.mention))
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(
        channel=f"<#{channel_id}>")
        for channel_id, message_id in state["posts"])

    async def recheck() -> bool:
        try:
            current = await workflow.bulk_add_roster_preview(values["roster_id"], raw_tags)
            if current != state:
                return False
            if role is not None:
                check_role(role, context.guild, context.guild.me, {})
            for member in members.values():
                check_member(member, context.guild.me)
            for channel in channels:
                check_post_access(channel, context.member, context.guild.me)
        except (ValueError, LookupError):
            return False
        return True

    async def run() -> ActionOutcome:
        result = await workflow.bulk_add_roster_tags(values["roster_id"], raw_tags)
        return ActionOutcome("complete", "private", text=result.message)

    context.state.proposed_changes.append(PreparedAction(
        "bulk_add_roster_accounts", {"roster_id": roster.id},
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_BULK_LABEL,
                      detail_access=frozenset({ACCESS_LEAD_PLUS})),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}


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
        raise ValueError('That roster is unavailable.')
    roster_id = values["roster_id"]
    member_id = values.get("member_id") or context.member.id
    member = await resolve_member(context.guild, member_id)
    check_member(member, context.guild.me)
    state = await workflow.prepare_roster_account_selection(
        roster_id, member_id, mode=mode,
        for_other_member=member_id != context.member.id)
    roster, picker = state
    if roster is None or roster.guild_id != context.guild.id or not picker.accounts:
        return {"status": "needs_input", "issue": picker.message if picker else 'That roster is unavailable.',
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
    channels = [await resolve_channel(context, channel_id)
                for channel_id, _ in posts]
    for channel in channels:
        check_post_access(channel, context.member, context.guild.me)
    lines = [ACTION_ROSTER_ACCOUNT_LINE.format(
        action="Sign up" if mode == "signup" else "Remove",
        member=member.mention, name=roster.name)]
    lines.extend(ACTION_ROSTER_ACCOUNT_ITEM.format(
        name=account.player_name, tag=account.player_tag)
        for account in selected_accounts)
    if role is not None:
        lines.append(ACTION_ROSTER_ACCOUNT_ROLE.format(role=role.mention))
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(
        channel=f"<#{channel_id}>")
        for channel_id, message_id in posts)
    snapshots = {account.player_tag: account for account in picker.accounts}

    async def recheck() -> bool:
        try:
            check_member(member, context.guild.me)
            if role is not None:
                check_role(role, context.guild, context.guild.me, {})
            for channel in channels:
                check_post_access(channel, context.member, context.guild.me)
            current_roster, current = await workflow.prepare_roster_account_selection(
                roster_id, member_id, mode=mode,
                for_other_member=member_id != context.member.id)
            if current_roster != roster or current is None:
                return False
            return all(snapshots[tag] in current.accounts for tag in selected)
        except (discord.DiscordException, ValueError, RuntimeError, KeyError, TypeError, OSError):
            return False

    async def run() -> ActionOutcome:
        result = await workflow.change_roster_accounts(
            roster_id, member_id=member_id, player_tags=selected,
            mode=mode, account_snapshots=snapshots,
            bypass_min_townhall=member_id != context.member.id,
        )
        if not result.changed:
            raise ValueError(result.message)
        return ActionOutcome("complete", "private", text=result.message,
                              after={"accounts": selected})

    context.state.proposed_changes.append(PreparedAction(
        "signup_roster_accounts" if mode == "signup" else "remove_roster_accounts",
        {"roster_id": roster_id, "member_id": member_id, "accounts": selected},
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_ACCOUNT_LABEL,
                      detail_access=(frozenset({ACCESS_LEAD_PLUS})
                                     if member_id != context.member.id else frozenset())),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}
