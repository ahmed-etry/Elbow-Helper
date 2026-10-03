"""Rosters setup commands."""

from __future__ import annotations
from collections.abc import Mapping
from typing import Any
from elbow_helper.features.agent.discord_actions.safety import check_member, check_role
from elbow_helper.features.rosters.config import DEFAULT_MAX_MEMBERS
from elbow_helper.features.rosters import settings as roster_settings
from ...access import ACCESS_LEAD_PLUS
from ...actions.contracts import ActionRefused, ActionClass, ChangePreview
from ...wording import (
    ACTION_ROSTER_CREATE_CLAN,
    ACTION_ROSTER_CREATE_LABEL,
    ACTION_ROSTER_CREATE_LINE,
    ACTION_ROSTER_CREATE_LIMIT,
    ACTION_SIGNUP_ROLE,
    ACTION_ROSTER_CLONE_CONTROLS,
    ACTION_ROSTER_CLONE_LABEL,
    ACTION_ROSTER_CLONE_LINE,
    ACTION_ROSTER_CLONE_MINIMUM,
    ACTION_ROSTER_CLONE_RESET,
    ACTION_ROSTER_CLONE_SCHEDULE,
    ACTION_ROSTER_CLONE_START,
    ACTION_ROSTER_NO_ROLE,
    ACTION_VALUE_YES,
    ACTION_VALUE_NO,
    ACTION_VALUE_HIDDEN,
    ACTION_VALUE_VISIBLE,
    ACTION_VALUE_OFF,
    ACTION_ROSTER_DELETE_LABEL,
    ACTION_ROSTER_DELETE_LINE,
    ACTION_MEMBER_LINE,
    ACTION_ROSTER_DELETE_POST,
    ACTION_ROSTER_DELETE_ROLE,
    ACTION_ROSTER_DELETE_HISTORY,
    ACTION_ROSTER_CYCLE_ONE,
    ACTION_ROSTER_CYCLE_MANY,
    ACTION_ROSTER_SIGNUP_ONE,
    ACTION_ROSTER_SIGNUP_MANY,
    ACTION_FIELD_CHANGE,
    ACTION_ROSTER_EDIT_FIELDS,
    ACTION_ROSTER_EDIT_LABEL,
    ACTION_ROSTER_EDIT_LINE,
    ACTION_ROSTER_POST_REFRESH,
    ACTION_ROSTER_EDIT_ROLE_SYNC,
)
from ...actions.outcomes import ActionOutcome
from ...commands.registry import CommandAdapter, PreparedCommandChange


def _setup_values(**values: Any) -> dict[str, Any]:
    try:
        return roster_settings.validate_roster_setup(**values)
    except ValueError as error:
        raise ActionRefused(str(error)) from error


async def _source_roster(workflow, guild_id, identifier):
    try:
        return await roster_settings.require_guild_roster(workflow, guild_id, identifier)
    except ValueError as error:
        raise ActionRefused(str(error)) from error


async def _available_name(workflow, guild_id, name) -> None:
    try:
        await roster_settings.require_roster_name_available(workflow, guild_id, name)
    except ValueError as error:
        raise ActionRefused(str(error)) from error


async def prepare_roster_create(context: Any,
                                values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ActionRefused('That roster is unavailable.')
    setup = _setup_values(name=values["name"],
                          max_members=values.get("max_members", DEFAULT_MAX_MEMBERS))
    name, max_members = setup["name"], setup["max_members"]
    role_id = values.get("signup_role")
    role = context.guild.get_role(role_id) if role_id else None
    if role_id:
        check_role(role, context.guild, context.guild.me, {}, requester=context.member)
    await _available_name(workflow, context.guild.id, name)
    clan_code = values["clan"]
    lines = [
        ACTION_ROSTER_CREATE_LINE.format(name=name),
        ACTION_ROSTER_CREATE_CLAN.format(clan=clan_code),
        ACTION_ROSTER_CREATE_LIMIT.format(count=max_members),
        ACTION_SIGNUP_ROLE.format(role=role.mention if role else ACTION_ROSTER_NO_ROLE),
    ]

    async def recheck() -> bool:
        if role_id:
            live_role = context.guild.get_role(role_id)
            try:
                check_role(live_role, context.guild, context.guild.me, {}, requester=context.member)
            except ValueError:
                return False
        return await workflow.roster_name_available(context.guild.id, name)

    async def run() -> ActionOutcome:
        roster = await workflow.create_roster(
            guild_id=context.guild.id, name=name, clan_code=clan_code,
            role_id=role_id, max_members=max_members,
        )
        if await workflow.get_roster(roster.id) is None:
            raise OSError("Roster creation could not be verified")
        return ActionOutcome(
            "complete", "private", text=roster_settings.roster_setup_confirmation("create", roster),
            result={"roster_id": roster.id}, after={"roster_id": roster.id},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck,
                      summary=ACTION_ROSTER_CREATE_LABEL,
                      detail_access=frozenset({ACCESS_LEAD_PLUS})),
        run,
    )


async def run_roster_create(context: Any,
                            values: Mapping[str, Any]) -> ActionOutcome:
    return await (await prepare_roster_create(context, values)).run()


async def prepare_roster_clone(context: Any,
                               values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ActionRefused('That roster is unavailable.')
    source = await _source_roster(workflow, context.guild.id, values["roster"])
    source_id = source.id
    setup = _setup_values(name=values["name"], max_members=values.get("max_members"),
                          min_townhall=values.get("min_townhall"))
    name = setup["name"]
    await _available_name(workflow, context.guild.id, name)
    role_id = values.get("signup_role")
    max_members = setup.get("max_members")
    min_townhall = setup.get("min_townhall")
    options = {
        "name": name, "clan_code": values.get("clan"),
        "role_id": role_id,
        "max_members": max_members,
        "min_townhall": min_townhall,
    }
    settings = workflow.roster_clone_settings(source, **options)
    effective_role_id = settings["role_id"]
    role = context.guild.get_role(effective_role_id) if effective_role_id else None
    if effective_role_id:
        check_role(role, context.guild, context.guild.me, {}, requester=context.member)
    try:
        roster_settings.validate_roster_clone_settings(settings)
    except ValueError as error:
        raise ActionRefused(str(error)) from error
    schedule = (
        f"{settings['open_day']} {settings['open_time']} to "
        f"{settings['close_day']} {settings['close_time']} "
        f"({settings['schedule_utc_offset']})"
        if settings["schedule_enabled"] else ACTION_VALUE_OFF
    )
    lines = (
        ACTION_ROSTER_CLONE_LINE.format(name=name, source=source.name),
        ACTION_ROSTER_CREATE_CLAN.format(clan=settings["clan_code"]),
        ACTION_ROSTER_CREATE_LIMIT.format(count=settings["max_members"]),
        ACTION_SIGNUP_ROLE.format(role=role.mention if role else ACTION_ROSTER_NO_ROLE),
        ACTION_ROSTER_CLONE_MINIMUM.format(
            minimum=settings["min_townhall"] or ACTION_ROSTER_NO_ROLE,
        ),
        ACTION_ROSTER_CLONE_SCHEDULE.format(schedule=schedule),
        ACTION_ROSTER_CLONE_RESET.format(
            value=ACTION_VALUE_YES if settings["reset_on_open"] else ACTION_VALUE_NO,
        ),
        ACTION_ROSTER_CLONE_CONTROLS.format(
            value=ACTION_VALUE_HIDDEN if settings["buttons_hidden"] else ACTION_VALUE_VISIBLE,
        ),
        ACTION_ROSTER_CLONE_START,
    )

    async def recheck() -> bool:
        current = await workflow.get_roster(source_id)
        if current != source or not await workflow.roster_name_available(context.guild.id, name):
            return False
        if effective_role_id:
            try:
                check_role(context.guild.get_role(effective_role_id),
                           context.guild, context.guild.me, {}, requester=context.member)
            except ValueError:
                return False
        return True

    async def run() -> ActionOutcome:
        clone = await workflow.clone_roster(source, **options)
        if await workflow.get_roster(clone.id) is None:
            raise OSError("Roster clone could not be verified")
        return ActionOutcome(
            "complete", "private",
            text=roster_settings.roster_setup_confirmation("clone", clone, source=source),
            result={"roster_id": clone.id}, after={"roster_id": clone.id},
        )

    return PreparedCommandChange(
        ChangePreview(lines[:1], recheck,
                      summary=ACTION_ROSTER_CLONE_LABEL, details=lines[1:],
                      detail_access=frozenset({ACCESS_LEAD_PLUS})), run,
    )


async def run_roster_clone(context: Any,
                           values: Mapping[str, Any]) -> ActionOutcome:
    return await (await prepare_roster_clone(context, values)).run()


async def prepare_roster_delete(context: Any,
                                values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ActionRefused('That roster is unavailable.')
    roster = await _source_roster(workflow, context.guild.id, values["roster"])
    roster_id = roster.id
    state = await workflow.roster_deletion_state(roster)

    def check_targets() -> None:
        role = context.guild.get_role(roster.role_id) if roster.role_id else None
        if role is not None:
            check_role(role, context.guild, context.guild.me, {}, requester=context.member)
        for member_id in state["member_ids"]:
            member = context.guild.get_member(member_id)
            if member is not None:
                check_member(member, context.guild.me,
                    requester=context.member, guild=context.guild)

    check_targets()
    lines = [ACTION_ROSTER_DELETE_LINE.format(name=roster.name)]
    cycle_count = len(state["history"])
    signup_count = sum(len(members) for _, members in state["history"])
    lines.append(ACTION_ROSTER_DELETE_HISTORY.format(
        cycle_count=cycle_count,
        cycle_word=ACTION_ROSTER_CYCLE_ONE if cycle_count == 1 else ACTION_ROSTER_CYCLE_MANY,
        signup_count=signup_count,
        signup_word=ACTION_ROSTER_SIGNUP_ONE if signup_count == 1 else ACTION_ROSTER_SIGNUP_MANY,
    ))
    if roster.role_id and state["member_ids"]:
        lines.append(ACTION_ROSTER_DELETE_ROLE.format(role=f"<@&{roster.role_id}>"))
    lines.extend(ACTION_MEMBER_LINE.format(member=f"<@{member_id}>")
                 for member_id in state["member_ids"])
    lines.extend(ACTION_ROSTER_DELETE_POST.format(
        channel=f"<#{channel_id}>", ) for channel_id, message_id in state["posts"])

    async def recheck() -> bool:
        current = await workflow.get_roster(roster_id)
        if current != roster:
            return False
        try:
            check_targets()
        except ValueError:
            return False
        return await workflow.roster_deletion_state(roster) == state

    async def run() -> ActionOutcome:
        await workflow.delete_roster(roster)
        if await workflow.get_roster(roster_id) is not None:
            raise OSError("Roster deletion could not be verified")
        return ActionOutcome(
            "complete", "private", text=roster_settings.roster_setup_confirmation("delete", roster),
            after={"roster_id": roster.id, "deleted": True},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck,
                      summary=ACTION_ROSTER_DELETE_LABEL,
                      detail_access=frozenset({ACCESS_LEAD_PLUS}),
                      before={"roster_id": roster.id}),
        run,
    )


async def run_roster_delete(context: Any,
                            values: Mapping[str, Any]) -> ActionOutcome:
    return await (await prepare_roster_delete(context, values)).run()


async def prepare_roster_edit(context: Any,
                              values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ActionRefused('That roster is unavailable.')
    roster = await _source_roster(workflow, context.guild.id, values["roster"])
    roster_id = roster.id
    max_members = values.get("max_members")
    min_townhall = values.get("min_townhall")
    changes, issue = workflow.roster_edit_changes(
        name=values.get("name"), clan_code=values.get("clan"),
        role_id=values.get("signup_role"),
        max_members=max_members, min_townhall=min_townhall,
        remove_signup_role=bool(values.get("remove_signup_role", False)),
    )
    if issue:
        raise ActionRefused(issue)
    if ("name" in changes
            and changes["name"].casefold() != roster.name.casefold()):
        await _available_name(workflow, context.guild.id, changes["name"])
    state = await workflow.roster_edit_state(roster)
    if ("max_members" in changes
            and changes["max_members"] < state["account_count"]):
        raise ActionRefused(workflow.roster_capacity_issue(state["account_count"]))

    def check_targets() -> None:
        if "role_id" not in changes or changes["role_id"] == roster.role_id:
            return
        for role_id in (roster.role_id, changes["role_id"]):
            if role_id is not None:
                check_role(context.guild.get_role(role_id),
                           context.guild, context.guild.me, {}, requester=context.member)
        for member_id in state["member_ids"]:
            member = context.guild.get_member(member_id)
            if member is not None:
                check_member(member, context.guild.me,
                    requester=context.member, guild=context.guild)

    check_targets()

    def display(key: str, value: object) -> str:
        if key == "role_id":
            return f"<@&{value}>" if value is not None else ACTION_ROSTER_NO_ROLE
        return str(value) if value is not None else ACTION_ROSTER_NO_ROLE

    lines = [ACTION_ROSTER_EDIT_LINE.format(name=roster.name)]
    details = tuple(ACTION_FIELD_CHANGE.format(
        field=ACTION_ROSTER_EDIT_FIELDS[key],
        old=display(key, getattr(roster, key)), new=display(key, new),
    ) for key, new in changes.items())
    if "role_id" in changes and changes["role_id"] != roster.role_id:
        lines.extend(ACTION_ROSTER_EDIT_ROLE_SYNC.format(member=f"<@{member_id}>")
                     for member_id in state["member_ids"])
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(
        channel=f"<#{channel_id}>", ) for channel_id, message_id in state["posts"])

    async def recheck() -> bool:
        current = await workflow.get_roster(roster_id)
        if current != roster:
            return False
        if ("name" in changes
                and changes["name"].casefold() != roster.name.casefold()
                and not await workflow.roster_name_available(
                    context.guild.id, changes["name"],
                )):
            return False
        try:
            check_targets()
        except ValueError:
            return False
        return await workflow.roster_edit_state(roster) == state

    async def run() -> ActionOutcome:
        updated = await workflow.update_roster_settings(roster, changes)
        actual = await workflow.get_roster(roster_id)
        if actual is None or any(getattr(actual, key) != value
                                 for key, value in changes.items()):
            raise OSError("Roster update could not be verified")
        return ActionOutcome(
            "complete", "private", text=roster_settings.roster_setup_confirmation("edit", updated),
            after={"roster_id": roster_id, "changes": changes},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck,
                      summary=ACTION_ROSTER_EDIT_LABEL, details=details,
                      detail_access=frozenset({ACCESS_LEAD_PLUS}),
                      before={"roster_id": roster.id,
                              "values": {key: getattr(roster, key) for key in changes}}),
        run,
    )


async def run_roster_edit(context: Any,
                          values: Mapping[str, Any]) -> ActionOutcome:
    return await (await prepare_roster_edit(context, values)).run()


def roster_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter("/roster create", "confirm", run_roster_create,
                       prepare=prepare_roster_create,
                       action_class=ActionClass.CHANGE,
                       entity_options=(("signup_role", "discord_role"),)),
        CommandAdapter("/roster clone", "confirm", run_roster_clone,
                       prepare=prepare_roster_clone,
                       action_class=ActionClass.CHANGE,
                       entity_options=(("roster", "roster"),
                                       ("signup_role", "discord_role"))),
        CommandAdapter("/roster delete", "confirm", run_roster_delete,
                       prepare=prepare_roster_delete,
                       action_class=ActionClass.IRREVERSIBLE,
                       entity_options=(("roster", "roster"),)),
        CommandAdapter("/roster edit", "confirm", run_roster_edit,
                       prepare=prepare_roster_edit,
                       action_class=ActionClass.CHANGE,
                       entity_options=(("roster", "roster"),
                                       ("signup_role", "discord_role"))),
    )
