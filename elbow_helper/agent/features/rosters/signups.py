"""Confirmed roster signup and removal actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.ai import AgentToolDefinition

from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.safety.access import ACCESS_LEAD_PLUS, require_evidence_access
from elbow_helper.agent.actions.contracts import ActionRefused, ActionClass, ChangePreview, PreparedAction
from elbow_helper.agent.actions.outcomes import ActionOutcome
from elbow_helper.agent.engine.models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from elbow_helper.agent.conversation.wording import ACTION_ROSTER_ACCOUNT_LABEL, ACTION_ROSTER_BULK_LABEL, ACTION_ROSTER_REMOVE_LABEL, ACTION_UNAVAILABLE
from elbow_helper.agent.discord_actions.safety import check_member, check_role, present_members, resolve_member
from .management import (
    can_manage_roster, check_roster_posts,
    require_roster_management_access,
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
            description=(
                "Perform the Sign Up button's action on an open roster post, including CWL signup, "
                "for the requester's selected linked Clash accounts or another member's "
                "when the roster's rules allow it."
            ),
            parameters=schema,
        ), prepare_roster_signup, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(entity_fields=(
                    ("roster_id", "roster"),
                    ("member_id", "discord_member"),
                    ("accounts", "clash_account_set"),
                ), source_scope="request_context"),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="remove_roster_accounts",
            description=(
                "Perform the Opt-out button's action on an open roster post, including CWL signup, "
                "for the requester's selected linked Clash accounts or another member's "
                "when the roster's rules allow it."
            ),
            parameters=schema,
        ), prepare_roster_removal, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(entity_fields=(
                    ("roster_id", "roster"),
                    ("member_id", "discord_member"),
                    ("accounts", "clash_account_set"),
                ), source_scope="request_context"),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="bulk_add_roster_accounts",
            description="Add linked Clash accounts to an open roster by player tag.",
            parameters={"type": "object", "properties": {
                "roster_id": {"type": "integer", "minimum": 1},
                "player_tags": {"type": "array", "items": {"type": "string"},
                                "minItems": 1, "uniqueItems": True},
            }, "required": ["roster_id", "player_tags"], "additionalProperties": False},
        ), prepare_bulk_roster_add, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(
                entity_fields=(("roster_id", "roster"), ("player_tags", "clash_account_set")),
                source_scope="request_context",
            ),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="remove_roster_signup_rows",
            description="Remove any selected current signup rows from a roster.",
            parameters={"type": "object", "properties": {
                "roster_id": {"type": "integer", "minimum": 1},
                "accounts": {"type": "array", "items": {"type": "string"},
                             "minItems": 1, "uniqueItems": True},
            }, "required": ["roster_id", "accounts"], "additionalProperties": False},
        ), prepare_roster_row_removal, AgentCapabilityEffect.COMMAND,
            ActionClass.CHANGE,
            contract=CapabilityContract(
                entity_fields=(("roster_id", "roster"), ("accounts", "clash_account_set")),
                source_scope="request_context",
            ),
        ),
    )


async def prepare_roster_row_removal(context: AgentRequestContext,
                                     values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    require_roster_management_access(context)
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ActionRefused('That roster is unavailable.')
    state = await workflow.roster_signed_rows(values["roster_id"])
    if state is None or state["roster"].guild_id != context.guild.id:
        raise ActionRefused('That roster is unavailable.')
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
                              if matches else "That account isn't signed up for this roster.")}
        if matches[0].player_tag not in {row.player_tag for row in selected}:
            selected.append(matches[0])
    role = context.guild.get_role(roster.role_id) if roster.role_id else None
    if role is not None:
        check_role(role, context.guild, context.guild.me, {}, requester=context.member)
    owners = {}
    for row in selected:
        owner = context.guild.get_member(row.discord_user_id)
        if owner is None:
            try:
                owner = await context.guild.fetch_member(row.discord_user_id)
            except discord.NotFound:
                owner = None
        if owner is not None:
            check_member(owner, context.guild.me, requester=context.member, guild=context.guild)
        owners[row.discord_user_id] = owner
    await check_roster_posts(context, state["posts"])
    selected_tags = {row.player_tag for row in selected}
    remaining = {row.discord_user_id for row in state["members"]
                 if row.player_tag not in selected_tags}
    losing_role = {
        member.id for member in owners.values()
        if role is not None and member is not None and member.id not in remaining
        and await workflow.roster_signup_role_changes(roster, member.id, should_have=False)
    }
    lines = [
        f"➖ Remove **{row.player_name}** `{row.player_tag}`, "
        f"signed up by <@{row.discord_user_id}>, from **{roster.name}**"
        for row in selected
    ]
    if role is not None:
        lines.extend(f"<@{member_id}> will lose {role.mention}."
                     for member_id in owners if member_id in losing_role)

    async def recheck() -> bool:
        current = await workflow.roster_signed_rows(values["roster_id"])
        if current != state:
            return False
        try:
            if role is not None:
                check_role(role, context.guild, context.guild.me, {}, requester=context.member)
            for owner in owners.values():
                if owner is not None:
                    check_member(owner, context.guild.me,
                        requester=context.member, guild=context.guild)
                    if role is not None and owner.id not in remaining:
                        loses_role = await workflow.roster_signup_role_changes(
                            roster, owner.id, should_have=False)
                        if loses_role != (owner.id in losing_role):
                            return False
            await check_roster_posts(context, current["posts"])
        except (discord.DiscordException, ValueError):
            return False
        return can_manage_roster(context)

    async def run() -> ActionOutcome:
        result = await workflow.remove_roster_signup_rows(
            values["roster_id"], [row.player_tag for row in selected])
        if not result.changed:
            raise ActionRefused(result.message)
        accounts = ", ".join(f"**{row.player_name}** `{row.player_tag}`"
                             for row in selected if row.player_tag in result.affected_tags)
        text = f"Removed {accounts} from **{roster.name}**."
        return ActionOutcome("complete", "private", text="\n".join((text, *result.notices)))

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
    if not can_manage_roster(context):
        return {"error": ACTION_UNAVAILABLE, "required_access": [ACCESS_LEAD_PLUS],
                "cause": "access_denied"}
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ActionRefused('That roster is unavailable.')
    raw_tags = " ".join(values["player_tags"])
    try:
        state = await workflow.bulk_add_roster_preview(values["roster_id"], raw_tags)
    except ValueError as exc:
        return {"status": "needs_input", "issue": str(exc)}
    roster = state["roster"]
    if roster.guild_id != context.guild.id:
        raise ActionRefused('That roster is unavailable.')
    if all(row[3] for row in state["accounts"]):
        return {"status": "no_change"}
    role = context.guild.get_role(roster.role_id) if roster.role_id else None
    if role is not None:
        check_role(role, context.guild, context.guild.me, {}, requester=context.member)
    members = await present_members(context.guild, dict.fromkeys(
        member_id for _, _, member_id, _ in state["accounts"]))
    for member in members.values():
        check_member(member, context.guild.me, requester=context.member, guild=context.guild)
    await check_roster_posts(context, state["posts"])
    lines = [f"📝 Sign up **{name}** `{tag}`, linked to <@{member_id}>, for **{roster.name}**"
             for tag, name, member_id, signed in state["accounts"] if not signed]
    details = tuple(f"**{name}** `{tag}`, linked to <@{member_id}>, is already signed up."
                    for tag, name, member_id, signed in state["accounts"] if signed)
    gaining_role = set()
    if role is not None:
        gaining_role = {member_id for member_id in state["adding_members"]
                        if await workflow.roster_signup_role_changes(
                            roster, member_id, should_have=True)}
        lines.extend(f"<@{member_id}> will get {role.mention}."
                     for member_id in state["adding_members"] if member_id in gaining_role)

    async def recheck() -> bool:
        if not can_manage_roster(context):
            return False
        try:
            current = await workflow.bulk_add_roster_preview(values["roster_id"], raw_tags)
            if current != state:
                return False
            if role is not None:
                check_role(role, context.guild, context.guild.me, {}, requester=context.member)
            for member in members.values():
                check_member(member, context.guild.me,
                    requester=context.member, guild=context.guild)
            if role is not None:
                for member_id in state["adding_members"]:
                    gains_role = await workflow.roster_signup_role_changes(
                        roster, member_id, should_have=True)
                    if gains_role != (member_id in gaining_role):
                        return False
            await check_roster_posts(context, current["posts"])
        except ValueError:
            return False
        return True

    async def run() -> ActionOutcome:
        result = await workflow.bulk_add_roster_tags(values["roster_id"], raw_tags)
        if not result.changed:
            raise ActionRefused(result.message)
        accounts = ", ".join(f"**{name}** `{tag}`" for tag, name, _, _ in state["accounts"]
                             if tag in result.affected_tags)
        text = f"Signed up {accounts} for **{roster.name}**."
        return ActionOutcome("complete", "private", text="\n".join((text, *result.notices)))

    context.state.proposed_changes.append(PreparedAction(
        "bulk_add_roster_accounts", {"roster_id": roster.id},
        ChangePreview((), recheck, summary=ACTION_ROSTER_BULK_LABEL, details=(*lines, *details),
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
        raise ActionRefused('That roster is unavailable.')
    roster_id = values["roster_id"]
    member_id = values.get("member_id") or context.member.id
    for_other_member = member_id != context.member.id
    if for_other_member:
        require_roster_management_access(context)
    member = await resolve_member(context.guild, member_id)
    check_member(member, context.guild.me, requester=context.member, guild=context.guild)
    state = await workflow.prepare_roster_account_selection(
        roster_id, member_id, mode=mode,
        for_other_member=for_other_member)
    roster, picker = state
    if roster is None or roster.guild_id != context.guild.id or not picker.accounts:
        return {"status": "needs_input", "issue": picker.message if picker else 'That roster is unavailable.'}
    selected, issue = workflow.resolve_roster_account_choices(
        picker.accounts, list(values["accounts"]))
    if issue:
        return {"status": "needs_input", "issue": issue,
                "available_accounts": [{"name": account.player_name, "tag": account.player_tag}
                                       for account in picker.accounts]}
    role = context.guild.get_role(roster.role_id) if roster.role_id else None
    if role is not None:
        check_role(role, context.guild, context.guild.me, {}, requester=context.member)
    selected_accounts = [account for account in picker.accounts
                         if account.player_tag in selected]
    posts = (await workflow.roster_edit_state(roster))["posts"]
    await check_roster_posts(context, posts)
    signing_up = mode == "signup"
    accounts = {account.player_tag: f"**{account.player_name}** `{account.player_tag}`"
                for account in selected_accounts}
    if signing_up:
        owner = f"{member.mention}'s " if for_other_member else ""
        lines = [f"📝 Sign up {owner}{account} for **{roster.name}**"
                 for account in accounts.values()]
    else:
        lines = [f"➖ Remove {member.mention}'s {account} from **{roster.name}**"
                 for account in accounts.values()]

    async def role_changes(picker) -> bool:
        return (role is not None
                and (signing_up or all(account.player_tag in selected for account in picker.accounts))
                and await workflow.roster_signup_role_changes(
                    roster, member_id, should_have=signing_up))

    changes_role = await role_changes(picker)
    if changes_role:
        verb = "get" if signing_up else "lose"
        who = f"{member.mention} will" if for_other_member else "You'll"
        lines.append(f"{who} {verb} {role.mention}")
    snapshots = {account.player_tag: account for account in picker.accounts}

    async def recheck() -> bool:
        if for_other_member and not can_manage_roster(context):
            return False
        try:
            check_member(member, context.guild.me, requester=context.member, guild=context.guild)
            if role is not None:
                check_role(role, context.guild, context.guild.me, {}, requester=context.member)
            await check_roster_posts(context, posts)
            current_roster, current = await workflow.prepare_roster_account_selection(
                roster_id, member_id, mode=mode,
                for_other_member=for_other_member)
            if current_roster != roster or current is None:
                return False
            return (all(snapshots[tag] in current.accounts for tag in selected)
                    and await role_changes(current) == changes_role)
        except (discord.DiscordException, ValueError):
            return False

    async def run() -> ActionOutcome:
        result = await workflow.change_roster_accounts(
            roster_id, member_id=member_id, player_tags=selected,
            mode=mode, account_snapshots=snapshots,
            bypass_min_townhall=for_other_member,
        )
        if not result.changed:
            raise ActionRefused(result.message)
        text = [
            (f"{accounts[tag]} is signed up for **{roster.name}**." if signing_up
             else f"Removed {accounts[tag]} from **{roster.name}**.")
            for tag in result.affected_tags
        ]
        return ActionOutcome("complete", "private", text="\n".join((*text, *result.notices)),
                              after={"accounts": list(result.affected_tags)})

    context.state.proposed_changes.append(PreparedAction(
        "signup_roster_accounts" if mode == "signup" else "remove_roster_accounts",
        {"roster_id": roster_id, "member_id": member_id, "accounts": selected},
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_ACCOUNT_LABEL,
                      detail_access=(frozenset({ACCESS_LEAD_PLUS})
                                     if member_id != context.member.id else frozenset())),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}
