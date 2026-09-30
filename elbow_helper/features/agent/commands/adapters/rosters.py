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
    ACTION_ROSTER_NAME_TAKEN, ACTION_ROSTER_NO_ROLE,
    ACTION_ROSTER_UNAVAILABLE,
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


def roster_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/roster create", "confirm", run_roster_create,
                           prepare=prepare_roster_create,
                           action_class=ActionClass.CHANGE,
                           entity_options=(("signup_role", "discord_role"),)),)
