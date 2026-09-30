"""Roster commands through public roster operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.features.agent.tools.discord_safety import check_role
from elbow_helper.features.rosters.config import DEFAULT_MAX_MEMBERS, MAX_ROSTER_MEMBERS

from ...actions.contracts import ActionClass, ChangePreview
from ...wording import (
    ACTION_ROSTER_CREATE_CLAN, ACTION_ROSTER_CREATE_LABEL,
    ACTION_ROSTER_CREATE_LINE, ACTION_ROSTER_CREATE_LIMIT,
    ACTION_ROSTER_CREATE_NAME_INVALID, ACTION_ROSTER_CREATE_ROLE,
    ACTION_ROSTER_CLONE_CONTROLS, ACTION_ROSTER_CLONE_LABEL,
    ACTION_ROSTER_CLONE_LINE, ACTION_ROSTER_CLONE_MINIMUM,
    ACTION_ROSTER_CLONE_RESET, ACTION_ROSTER_CLONE_SCHEDULE,
    ACTION_ROSTER_CLONE_START, ACTION_ROSTER_NAME_TAKEN, ACTION_ROSTER_NO_ROLE,
    ACTION_ROSTER_UNAVAILABLE, ACTION_VALUE_YES, ACTION_VALUE_NO,
    ACTION_VALUE_HIDDEN, ACTION_VALUE_VISIBLE, ACTION_VALUE_OFF,
)
from ..outcomes import CommandOutcome
from ..registry import CommandAdapter, PreparedCommandChange


async def prepare_roster_create(context: Any,
                                values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    name = workflow.validate_roster_name(values["name"])
    if name is None:
        raise ValueError(ACTION_ROSTER_CREATE_NAME_INVALID)
    max_members = int(values.get("max_members", DEFAULT_MAX_MEMBERS))
    if not 1 <= max_members <= MAX_ROSTER_MEMBERS:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    role_id = values.get("signup_role")
    role = context.guild.get_role(role_id) if role_id else None
    if role_id:
        check_role(role, context.guild, context.guild.me, {})
    if not await workflow.roster_name_available(context.guild.id, name):
        raise ValueError(ACTION_ROSTER_NAME_TAKEN)
    clan_code = values["clan"]
    lines = [
        ACTION_ROSTER_CREATE_LINE.format(name=name),
        ACTION_ROSTER_CREATE_CLAN.format(clan=clan_code),
        ACTION_ROSTER_CREATE_LIMIT.format(count=max_members),
        ACTION_ROSTER_CREATE_ROLE.format(role=role.mention if role else ACTION_ROSTER_NO_ROLE),
    ]

    async def recheck() -> bool:
        if role_id:
            live_role = context.guild.get_role(role_id)
            try:
                check_role(live_role, context.guild, context.guild.me, {})
            except ValueError:
                return False
        return await workflow.roster_name_available(context.guild.id, name)

    async def run() -> CommandOutcome:
        roster = await workflow.create_roster(
            guild_id=context.guild.id, name=name, clan_code=clan_code,
            role_id=role_id, max_members=max_members,
        )
        if await workflow.get_roster(roster.id) is None:
            raise OSError("Roster creation could not be verified")
        return CommandOutcome(
            "complete", "private", text=f"Created **{roster.name}**.",
            result={"roster_id": roster.id}, after={"roster_id": roster.id},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_CREATE_LABEL),
        run,
    )


async def run_roster_create(context: Any,
                            values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_roster_create(context, values)).run()


async def prepare_roster_clone(context: Any,
                               values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    try:
        source_id = int(values["roster"])
    except (TypeError, ValueError):
        raise ValueError(ACTION_ROSTER_UNAVAILABLE) from None
    source = await workflow.get_roster(source_id)
    if source is None or source.guild_id != context.guild.id:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    name = workflow.validate_roster_name(values["name"])
    if name is None:
        raise ValueError(ACTION_ROSTER_CREATE_NAME_INVALID)
    if not await workflow.roster_name_available(context.guild.id, name):
        raise ValueError(ACTION_ROSTER_NAME_TAKEN)
    role_id = values.get("signup_role")
    max_members = values.get("max_members")
    min_townhall = values.get("min_townhall")
    if max_members is not None and not 1 <= int(max_members) <= MAX_ROSTER_MEMBERS:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    if min_townhall is not None and int(min_townhall) < 0:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    options = {
        "name": name, "clan_code": values.get("clan"),
        "role_id": role_id,
        "max_members": int(max_members) if max_members is not None else None,
        "min_townhall": int(min_townhall) if min_townhall is not None else None,
    }
    settings = workflow.roster_clone_settings(source, **options)
    effective_role_id = settings["role_id"]
    role = context.guild.get_role(effective_role_id) if effective_role_id else None
    if effective_role_id:
        check_role(role, context.guild, context.guild.me, {})
    if settings["schedule_enabled"] and not all(
        settings[key] for key in (
            "open_day", "open_time", "close_day", "close_time",
            "schedule_utc_offset",
        )
    ):
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
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
        ACTION_ROSTER_CREATE_ROLE.format(role=role.mention if role else ACTION_ROSTER_NO_ROLE),
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
                           context.guild, context.guild.me, {})
            except ValueError:
                return False
        return True

    async def run() -> CommandOutcome:
        clone = await workflow.clone_roster(source, **options)
        if await workflow.get_roster(clone.id) is None:
            raise OSError("Roster clone could not be verified")
        return CommandOutcome(
            "complete", "private",
            text=f"Created **{clone.name}** from **{source.name}**.",
            result={"roster_id": clone.id}, after={"roster_id": clone.id},
        )

    return PreparedCommandChange(
        ChangePreview(lines, recheck, summary=ACTION_ROSTER_CLONE_LABEL), run,
    )


async def run_roster_clone(context: Any,
                           values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_roster_clone(context, values)).run()


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
    )
