"""Confirmed actions for standalone roster management controls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any
import discord

from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.features.rosters.config import ROSTER_PLAYER_COLUMN_MIN_WIDTH, ROSTER_PLAYER_COLUMN_MAX_WIDTH, ROSTER_DISCORD_COLUMN_MIN_WIDTH, ROSTER_DISCORD_COLUMN_MAX_WIDTH

from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.safety.access import ACCESS_LEAD_PLUS, has_access_requirements, require_evidence_access
from elbow_helper.agent.actions.contracts import ActionRefused, ActionClass, ChangePreview, PreparedAction
from elbow_helper.agent.actions.outcomes import ActionOutcome
from elbow_helper.agent.actions.values import display_value
from elbow_helper.agent.engine.models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from elbow_helper.agent.conversation.wording import ACTION_ROSTER_POST_REFRESH, ACTION_ROSTER_CONTROL_OPEN, ACTION_ROSTER_CONTROL_CLOSE, ACTION_ROSTER_CONTROL_SHOW, ACTION_ROSTER_CONTROL_HIDE, ACTION_ROSTER_CONTROL_CLEAR, ACTION_FIELD_CHANGE, ACTION_ROSTER_LAYOUT_LINE, ACTION_ROSTER_LAYOUT_LABEL, ACTION_ROSTER_REFRESH_LINE, ACTION_ROSTER_REFRESH_MEMBER, ACTION_ROSTER_REFRESH_LABEL, ACTION_ROSTER_LAYOUT_FIELDS
from elbow_helper.agent.discord_actions.safety import check_bot_post_access, check_member, check_role, present_members, resolve_channel
from elbow_helper.agent.conversation.wording import ACTION_UNAVAILABLE


ROSTER_MANAGEMENT_ACCESS = frozenset({ACCESS_LEAD_PLUS})


def can_manage_roster(context: AgentRequestContext) -> bool:
    return has_access_requirements(context.guild, context.member.id, ROSTER_MANAGEMENT_ACCESS)


def require_roster_management_access(context: AgentRequestContext) -> None:
    if not can_manage_roster(context):
        raise ActionRefused(ACTION_UNAVAILABLE, cause="access_denied")


def _layout_field(field: str) -> str:
    limits = {
        "player_width": (ROSTER_PLAYER_COLUMN_MIN_WIDTH, ROSTER_PLAYER_COLUMN_MAX_WIDTH),
        "discord_width": (ROSTER_DISCORD_COLUMN_MIN_WIDTH, ROSTER_DISCORD_COLUMN_MAX_WIDTH),
    }
    label = ACTION_ROSTER_LAYOUT_FIELDS[field]
    if field in limits:
        minimum, maximum = limits[field]
        return label.format(minimum=minimum, maximum=maximum)
    return label


async def check_roster_posts(context: AgentRequestContext,
                            posts: tuple[tuple[int, int], ...]) -> None:
    for channel_id, _ in posts:
        channel = await resolve_channel(context, channel_id)
        check_bot_post_access(channel, context.guild.me)


async def _check_members(context: AgentRequestContext,
                         roster: Any, member_ids: tuple[int, ...]) -> dict[int, Any]:
    role = context.guild.get_role(roster.role_id) if roster.role_id else None
    if role is not None:
        check_role(role, context.guild, context.guild.me, {}, requester=context.member)
    present = await present_members(context.guild, member_ids)
    for member in present.values():
        check_member(member, context.guild.me, requester=context.member, guild=context.guild)
    return present


_OPERATIONS = (
    ("open_roster", "open", ActionClass.CHANGE, ACTION_ROSTER_CONTROL_OPEN),
    ("close_roster", "close", ActionClass.CHANGE, ACTION_ROSTER_CONTROL_CLOSE),
    ("show_roster_controls", "toggle_buttons", ActionClass.CHANGE, ACTION_ROSTER_CONTROL_SHOW),
    ("hide_roster_controls", "toggle_buttons", ActionClass.CHANGE, ACTION_ROSTER_CONTROL_HIDE),
    ("clear_roster_signups", "clear", ActionClass.IRREVERSIBLE, ACTION_ROSTER_CONTROL_CLEAR),
)


def roster_management_tools() -> tuple[RegisteredAgentTool, ...]:
    schema = {"type": "object", "properties": {
        "roster_id": {"type": "integer", "minimum": 1},
    }, "required": ["roster_id"], "additionalProperties": False}
    async def prepare_state(context: AgentRequestContext,
                            values: Mapping[str, Any]) -> Mapping[str, Any]:
        selected = next((spec for spec in _OPERATIONS[:4]
                         if spec[0] == {
                             "open": "open_roster", "close": "close_roster",
                             "show": "show_roster_controls",
                             "hide": "hide_roster_controls",
                         }.get(values["operation"])), None)
        if selected is None:
            raise ActionRefused("Choose open, close, show or hide.")
        name, operation, classification, label = selected
        return await _prepare(context, values, name, operation, classification, label)

    tools = [RegisteredAgentTool(AgentToolDefinition(
        name="set_roster_state",
        description="Use the roster management controls for an authorized requester to open or close a roster or show or hide signup controls.",
        parameters={"type": "object", "properties": {
            "roster_id": {"type": "integer", "minimum": 1},
            "operation": {"type": "string", "enum": ["open", "close", "show", "hide"]},
        }, "required": ["roster_id", "operation"], "additionalProperties": False},
    ), prepare_state, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
        contract=CapabilityContract(
            entity_fields=(("roster_id", "roster"),), source_scope="request_context",
            filter_fields=("operation",),
        ),
             )]
    name, operation, classification, label = _OPERATIONS[4]
    async def prepare_clear(context: AgentRequestContext,
                            values: Mapping[str, Any]) -> Mapping[str, Any]:
        return await _prepare(context, values, name, operation, classification, label)
    tools.append(RegisteredAgentTool(
        AgentToolDefinition(
            name=name,
            description="Use the roster's Clear signups action for an authorized requester to remove all signups.",
            parameters=schema,
        ),
        prepare_clear, AgentCapabilityEffect.COMMAND, classification,
        contract=CapabilityContract(
            entity_fields=(("roster_id", "roster"),), source_scope="request_context",
        ),
    ))
    tools.append(RegisteredAgentTool(AgentToolDefinition(
        name="refresh_roster",
        description="Perform the roster refresh button's action for the requester to refresh account details, signup roles and the post.",
        parameters=schema,
    ), prepare_roster_refresh, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
        contract=CapabilityContract(
            entity_fields=(("roster_id", "roster"),), source_scope="request_context",
        ),
                 ))
    tools.append(RegisteredAgentTool(AgentToolDefinition(
        name="set_roster_layout",
        description="Set roster columns or displayed name lengths.",
        parameters={"type": "object", "properties": {
            "roster_id": {"type": "integer", "minimum": 1},
            "show_townhall": {"type": "boolean"},
            "show_discord": {"type": "boolean"},
            "show_clan": {"type": "boolean"},
            "player_width": {"type": "integer", "minimum": ROSTER_PLAYER_COLUMN_MIN_WIDTH,
                             "maximum": ROSTER_PLAYER_COLUMN_MAX_WIDTH},
            "discord_width": {"type": "integer", "minimum": ROSTER_DISCORD_COLUMN_MIN_WIDTH,
                              "maximum": ROSTER_DISCORD_COLUMN_MAX_WIDTH},
        }, "required": ["roster_id"], "additionalProperties": False},
    ), prepare_roster_layout, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
        contract=CapabilityContract(
            entity_fields=(("roster_id", "roster"),), source_scope="request_context",
            filter_fields=(
                "show_townhall",
                "show_discord",
                "show_clan",
                "player_width",
                "discord_width",
            )),
                 ))
    return tuple(tools)


async def prepare_roster_refresh(context: AgentRequestContext,
                                 values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    require_roster_management_access(context)
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ActionRefused('That roster is unavailable.')
    roster_id = values["roster_id"]
    state = await workflow.roster_management_state(roster_id)
    if state is None or state["roster"].guild_id != context.guild.id:
        raise ActionRefused('That roster is unavailable.')
    roster = state["roster"]
    await check_roster_posts(context, state["posts"])
    role = context.guild.get_role(roster.role_id) if roster.role_id else None
    if role is not None:
        check_role(role, context.guild, context.guild.me, {}, requester=context.member)
    members = list((await present_members(context.guild, state["member_ids"])).values())
    for member in members:
        check_member(member, context.guild.me, requester=context.member, guild=context.guild)
    lines = [ACTION_ROSTER_REFRESH_LINE.format(name=roster.name)]
    lines.extend(ACTION_ROSTER_REFRESH_MEMBER.format(member=member.mention)
                 for member in members)
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(channel=f"<#{channel_id}>")
                 for channel_id, message_id in state["posts"])

    async def recheck() -> bool:
        current = await workflow.roster_management_state(roster_id)
        if current is None or current != state:
            return False
        try:
            await check_roster_posts(context, state["posts"])
            if role is not None:
                check_role(role, context.guild, context.guild.me, {}, requester=context.member)
            for member in members:
                check_member(member, context.guild.me,
                    requester=context.member, guild=context.guild)
        except (discord.DiscordException, ValueError):
            return False
        return can_manage_roster(context)

    async def run() -> ActionOutcome:
        status = await workflow.refresh_roster(roster_id)
        if status != "complete":
            raise ActionRefused("That roster couldn't be refreshed.")
        return ActionOutcome("complete", "private", text=f"Refreshed **{roster.name}**.")

    context.state.proposed_changes.append(PreparedAction(
        "refresh_roster", dict(values),
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_REFRESH_LABEL,
                      detail_access=frozenset({ACCESS_LEAD_PLUS})),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}


async def prepare_roster_layout(context: AgentRequestContext,
                                values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    require_roster_management_access(context)
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ActionRefused('That roster is unavailable.')
    roster_id = values["roster_id"]
    state = await workflow.roster_layout_state(roster_id)
    if state is None or state[0].guild_id != context.guild.id:
        raise ActionRefused('That roster is unavailable.')
    roster, layout = state
    before = asdict(layout)
    changes = {field: values[field] for field in before if field in values
               and values[field] != before[field]}
    if not changes:
        return {"status": "no_change"}
    posts = (await workflow.roster_edit_state(roster))["posts"]
    await check_roster_posts(context, posts)
    lines = [ACTION_ROSTER_LAYOUT_LINE.format(action="Change", name=roster.name)]
    details = tuple(ACTION_FIELD_CHANGE.format(
        field=_layout_field(field), old=display_value(before[field], visibility=True),
        new=display_value(value, visibility=True))
        for field, value in changes.items())

    async def recheck() -> bool:
        live = await workflow.roster_layout_state(roster_id)
        if live is None or live[0] != roster or live[1] != layout:
            return False
        try:
            await check_roster_posts(context, posts)
        except (discord.DiscordException, ValueError):
            return False
        return can_manage_roster(context)

    async def run() -> ActionOutcome:
        updated, result = await workflow.set_roster_layout(roster_id, **changes)
        if updated is None:
            raise ActionRefused('That roster is unavailable.')
        return ActionOutcome("complete", "private",
                              text=f"Updated the layout of **{updated.name}**.",
                              after={"layout": asdict(result)})

    context.state.proposed_changes.append(PreparedAction(
        "set_roster_layout", {"roster_id": roster_id},
        ChangePreview((), recheck, summary=ACTION_ROSTER_LAYOUT_LABEL, details=(*lines, *details),
                      before={"layout": before},
                      detail_access=frozenset({ACCESS_LEAD_PLUS})), run,
    ))
    return {"status": "confirmation_required"}


async def prepare_roster_layout_undo(context: AgentRequestContext,
                                     log: Mapping[str, Any]) -> PreparedAction:
    require_roster_management_access(context)
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ActionRefused('That roster is unavailable.')
    roster_id = log["targets"]["roster_id"]
    state = await workflow.roster_layout_state(roster_id)
    if state is None or state[0].guild_id != context.guild.id:
        raise ActionRefused('That roster is unavailable.')
    roster, layout = state
    prior = log["before"]["layout"]
    expected = log["after"]["layout"]
    posts = (await workflow.roster_edit_state(roster))["posts"]
    await check_roster_posts(context, posts)
    lines = [ACTION_ROSTER_LAYOUT_LINE.format(action="Restore", name=roster.name)]
    details = tuple(ACTION_FIELD_CHANGE.format(
        field=_layout_field(field), old=display_value(getattr(layout, field), visibility=True),
        new=display_value(value, visibility=True))
        for field, value in prior.items() if getattr(layout, field) != value)

    async def recheck() -> bool:
        live = await workflow.roster_layout_state(roster_id)
        if live is None or asdict(live[1]) != expected:
            return False
        try:
            await check_roster_posts(context, posts)
        except (discord.DiscordException, ValueError):
            return False
        return can_manage_roster(context)

    async def run() -> ActionOutcome:
        updated, result = await workflow.set_roster_layout(roster_id, **prior)
        if updated is None:
            raise ActionRefused('That roster is unavailable.')
        return ActionOutcome("complete", "private",
                              text=f"Restored the layout of **{updated.name}**.",
                              after={"layout": asdict(result)})

    return PreparedAction(
        "undo_roster_layout", {"roster_id": roster_id},
        ChangePreview((), recheck, summary=ACTION_ROSTER_LAYOUT_LABEL, details=(*lines, *details),
                      before={"layout": expected},
                      detail_access=frozenset({ACCESS_LEAD_PLUS})), run,
    )


async def _prepare(context: AgentRequestContext, values: Mapping[str, Any],
                   name: str, operation: str, classification: ActionClass,
                   label: str) -> Mapping[str, Any]:
    await require_evidence_access(context)
    require_roster_management_access(context)
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ActionRefused('That roster is unavailable.')
    roster_id = values["roster_id"]
    state = await workflow.roster_management_state(roster_id)
    if state is None or state["roster"].guild_id != context.guild.id:
        raise ActionRefused('That roster is unavailable.')
    roster = state["roster"]
    await check_roster_posts(context, state["posts"])
    check_members = operation == "clear" or (operation == "open" and roster.reset_on_open)
    present = await _check_members(context, roster, state["member_ids"]) if check_members else {}
    if name == "show_roster_controls" and not roster.buttons_hidden:
        return {"status": "no_change"}
    if name == "hide_roster_controls" and roster.buttons_hidden:
        return {"status": "no_change"}
    count = state["account_count"]
    noun = "signup" if count == 1 else "signups"
    name_text = f"**{roster.name}**"
    if operation == "clear":
        lines = [f"➖ Clear {count} active {noun} from {name_text}"]
    elif operation == "open":
        lines = [f"📝 Open signups for {name_text}"]
    elif operation == "close":
        lines = [f"➖ Close signups for {name_text}"]
    else:
        verb = "Show" if roster.buttons_hidden else "Hide"
        lines = [f"⚙️ {verb} signup controls for {name_text}"]

    resets = operation == "open" and workflow.manual_open_resets_signups(roster)
    if resets:
        lines.append(f"This starts a new signup cycle and clears {count} active {noun}.")
    role = context.guild.get_role(roster.role_id) if check_members and roster.role_id else None

    async def role_losses() -> tuple[int, ...]:
        if role is None or operation != "clear":
            return ()
        losing = []
        for member_id in present:
            if await workflow.roster_signup_role_changes(roster, member_id, should_have=False):
                losing.append(member_id)
        return tuple(losing)

    losing_role = await role_losses()
    if losing_role:
        members = ", ".join(f"<@{member_id}>" for member_id in losing_role)
        lines.append(f"Remove {role.mention} from {members}.")

    def signature(snapshot: Mapping[str, Any]) -> tuple[Any, ...]:
        item = snapshot["roster"]
        return (item.guild_id, item.status, item.buttons_hidden, item.active_cycle_id,
                item.role_id, snapshot["account_count"], snapshot["member_ids"], snapshot["posts"])

    async def recheck() -> bool:
        current = await workflow.roster_management_state(roster_id)
        if current is None or signature(current) != signature(state):
            return False
        if operation == "open" and workflow.manual_open_resets_signups(current["roster"]) != resets:
            return False
        if await role_losses() != losing_role:
            return False
        try:
            await check_roster_posts(context, current["posts"])
            if check_members:
                await _check_members(context, current["roster"], current["member_ids"])
        except (discord.DiscordException, ValueError):
            return False
        return can_manage_roster(context)

    async def run() -> ActionOutcome:
        if operation == "clear":
            result = await workflow.clear_roster_signups(roster_id)
            text = ("\n".join((f"Cleared {count} {noun} from {name_text}.", *result.notices))
                    if result.changed else result.message)
        else:
            text = await workflow.change_roster_management(roster_id, operation)
            updated = await workflow.get_roster(roster_id)
            if updated is None:
                raise ActionRefused(text)
            if operation == "open" and updated.status == "open":
                text = f"Signups are open for {name_text}."
            elif operation == "close" and updated.status == "closed":
                text = f"Signups are closed for {name_text}."
            elif operation == "toggle_buttons":
                visible = "hidden" if updated.buttons_hidden else "visible"
                text = f"Signup controls for {name_text} are {visible}."
        return ActionOutcome("complete", "private", text=text)

    context.state.proposed_changes.append(PreparedAction(
        "clear_roster_signups" if operation == "clear" else "set_roster_state",
        {"roster_id": roster_id, **({"operation": values["operation"]}
                                  if operation != "clear" else {})},
        ChangePreview(tuple(lines), recheck,
                      summary=label, detail_access=frozenset({ACCESS_LEAD_PLUS})),
        run, action_class=classification,
    ))
    return {"status": "confirmation_required"}


UNDO_HANDLERS = {
    'set_roster_layout': prepare_roster_layout_undo,
    'undo_roster_layout': prepare_roster_layout_undo,
}
