"""Roster commands through public roster operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.features.agent.discord_actions.safety import (
    check_member, check_post_access, check_role, resolve_channel,
)
from elbow_helper.features.rosters.config import DEFAULT_MAX_MEMBERS, MAX_ROSTER_MEMBERS
from elbow_helper.features.help.discovery import ParameterInfo

from ...actions.contracts import ActionClass, ChangePreview
from ...models import AgentAttachment
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
    ACTION_ROSTER_TIMING_CLEAR,
    ACTION_ROSTER_TIMING_SET,
    ACTION_ROSTER_TIMING_RESET,
    ACTION_ROSTER_TIMING_KEEP,
    ACTION_ROSTER_TIMING_ROLE,
    ACTION_ROSTER_TIMING_LABEL,
    ACTION_ROSTER_TIMING_ROLE_KEEP,
    ACTION_ROSTER_SCHEDULE_DISABLE,
    ACTION_ROSTER_SCHEDULE_ENABLE,
    ACTION_ROSTER_SCHEDULE_RULE,
    ACTION_ROSTER_SCHEDULE_WINDOW,
    ACTION_ROSTER_SCHEDULE_RESET,
    ACTION_ROSTER_SCHEDULE_KEEP,
    ACTION_ROSTER_SCHEDULE_LABEL,
    ACTION_ROSTER_SCHEDULE_FIELDS,
    ACTION_VALUE_CURRENT,
    ACTION_VALUE_NEXT,
    ACTION_ROSTER_POST_ACCOUNTS,
    ACTION_ROSTER_POST_CHANNEL_OPTION,
    ACTION_ROSTER_POST_CLOSED,
    ACTION_ROSTER_POST_CONTROLS,
    ACTION_ROSTER_POST_HIDDEN,
    ACTION_ROSTER_POST_LABEL,
    ACTION_ROSTER_POST_LINE,
    ACTION_ROSTER_POST_OPEN,
    ACTION_ROSTER_POST_RESET,
    ACTION_ROSTER_POST_PAGE,
    ACTION_ROSTER_POST_IMAGE,
    ACTION_PREVIEW_BLANK,
    ACTION_ROSTER_EXPORT_ACCOUNT,
    ACTION_ROSTER_EXPORT_FILE,
    ACTION_ROSTER_EXPORT_GOOGLE,
    ACTION_ROSTER_EXPORT_LABEL,
    ACTION_ROSTER_EXPORT_LINE,
    ACTION_ROSTER_EXPORT_LINK,
)
from ...actions.outcomes import CommandOutcome, embed_text
from ..registry import CommandAdapter, PreparedCommandChange


async def prepare_roster_create(context: Any,
                                values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError('That roster is unavailable.')
    name = workflow.validate_roster_name(values["name"])
    if name is None:
        raise ValueError('Enter a roster name between 1 and 100 characters.')
    max_members = int(values.get("max_members", DEFAULT_MAX_MEMBERS))
    if not 1 <= max_members <= MAX_ROSTER_MEMBERS:
        raise ValueError('That roster is unavailable.')
    role_id = values.get("signup_role")
    role = context.guild.get_role(role_id) if role_id else None
    if role_id:
        check_role(role, context.guild, context.guild.me, {})
    if not await workflow.roster_name_available(context.guild.id, name):
        raise ValueError('A roster with that name already exists.')
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
        raise ValueError('That roster is unavailable.')
    try:
        source_id = int(values["roster"])
    except (TypeError, ValueError):
        raise ValueError('That roster is unavailable.') from None
    source = await workflow.get_roster(source_id)
    if source is None or source.guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    name = workflow.validate_roster_name(values["name"])
    if name is None:
        raise ValueError('Enter a roster name between 1 and 100 characters.')
    if not await workflow.roster_name_available(context.guild.id, name):
        raise ValueError('A roster with that name already exists.')
    role_id = values.get("signup_role")
    max_members = values.get("max_members")
    min_townhall = values.get("min_townhall")
    if max_members is not None and not 1 <= int(max_members) <= MAX_ROSTER_MEMBERS:
        raise ValueError('That roster is unavailable.')
    if min_townhall is not None and int(min_townhall) < 0:
        raise ValueError('That roster is unavailable.')
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
        raise ValueError('That roster is unavailable.')
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


async def prepare_roster_delete(context: Any,
                                values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError('That roster is unavailable.')
    try:
        roster_id = int(values["roster"])
    except (TypeError, ValueError):
        raise ValueError('That roster is unavailable.') from None
    roster = await workflow.get_roster(roster_id)
    if roster is None or roster.guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    state = await workflow.roster_deletion_state(roster)

    def check_targets() -> None:
        role = context.guild.get_role(roster.role_id) if roster.role_id else None
        if role is not None:
            check_role(role, context.guild, context.guild.me, {})
        for member_id in state["member_ids"]:
            member = context.guild.get_member(member_id)
            if member is not None:
                check_member(member, context.guild.me)

    check_targets()
    lines = [ACTION_ROSTER_DELETE_LINE.format(name=roster.name, roster_id=roster.id)]
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
        channel=f"<#{channel_id}>", message_id=message_id,
    ) for channel_id, message_id in state["posts"])

    async def recheck() -> bool:
        current = await workflow.get_roster(roster_id)
        if current != roster:
            return False
        try:
            check_targets()
        except ValueError:
            return False
        return await workflow.roster_deletion_state(roster) == state

    async def run() -> CommandOutcome:
        await workflow.delete_roster(roster)
        if await workflow.get_roster(roster_id) is not None:
            raise OSError("Roster deletion could not be verified")
        return CommandOutcome(
            "complete", "private", text=f"Deleted **{roster.name}**.",
            after={"roster_id": roster.id, "deleted": True},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_DELETE_LABEL,
                      before={"roster_id": roster.id}),
        run,
    )


async def run_roster_delete(context: Any,
                            values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_roster_delete(context, values)).run()


async def prepare_roster_edit(context: Any,
                              values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError('That roster is unavailable.')
    try:
        roster_id = int(values["roster"])
    except (TypeError, ValueError):
        raise ValueError('That roster is unavailable.') from None
    roster = await workflow.get_roster(roster_id)
    if roster is None or roster.guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    max_members = values.get("max_members")
    min_townhall = values.get("min_townhall")
    if max_members is not None and not 1 <= int(max_members) <= MAX_ROSTER_MEMBERS:
        raise ValueError('That roster is unavailable.')
    if min_townhall is not None and int(min_townhall) < 0:
        raise ValueError('That roster is unavailable.')
    changes, issue = workflow.roster_edit_changes(
        name=values.get("name"), clan_code=values.get("clan"),
        role_id=values.get("signup_role"),
        max_members=max_members, min_townhall=min_townhall,
        remove_signup_role=bool(values.get("remove_signup_role", False)),
    )
    if issue:
        raise ValueError(issue)
    if ("name" in changes
            and changes["name"].casefold() != roster.name.casefold()
            and not await workflow.roster_name_available(
                context.guild.id, changes["name"],
            )):
        raise ValueError('A roster with that name already exists.')
    state = await workflow.roster_edit_state(roster)
    if ("max_members" in changes
            and changes["max_members"] < state["account_count"]):
        raise ValueError(workflow.roster_capacity_issue(state["account_count"]))

    def check_targets() -> None:
        if "role_id" not in changes or changes["role_id"] == roster.role_id:
            return
        for role_id in (roster.role_id, changes["role_id"]):
            if role_id is not None:
                check_role(context.guild.get_role(role_id),
                           context.guild, context.guild.me, {})
        for member_id in state["member_ids"]:
            member = context.guild.get_member(member_id)
            if member is not None:
                check_member(member, context.guild.me)

    check_targets()

    def display(key: str, value: object) -> str:
        if key == "role_id":
            return f"<@&{value}>" if value is not None else ACTION_ROSTER_NO_ROLE
        return str(value) if value is not None else ACTION_ROSTER_NO_ROLE

    lines = [ACTION_ROSTER_EDIT_LINE.format(name=roster.name, roster_id=roster.id)]
    lines.extend(ACTION_FIELD_CHANGE.format(
        field=ACTION_ROSTER_EDIT_FIELDS[key],
        old=display(key, getattr(roster, key)), new=display(key, new),
    ) for key, new in changes.items())
    if "role_id" in changes and changes["role_id"] != roster.role_id:
        lines.extend(ACTION_ROSTER_EDIT_ROLE_SYNC.format(member=f"<@{member_id}>")
                     for member_id in state["member_ids"])
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(
        channel=f"<#{channel_id}>", message_id=message_id,
    ) for channel_id, message_id in state["posts"])

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

    async def run() -> CommandOutcome:
        updated = await workflow.update_roster_settings(roster, changes)
        actual = await workflow.get_roster(roster_id)
        if actual is None or any(getattr(actual, key) != value
                                 for key, value in changes.items()):
            raise OSError("Roster update could not be verified")
        return CommandOutcome(
            "complete", "private", text=f"Updated **{updated.name}**.",
            after={"roster_id": roster_id, "changes": changes},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_EDIT_LABEL,
                      before={"roster_id": roster.id,
                              "values": {key: getattr(roster, key) for key in changes}}),
        run,
    )


async def run_roster_edit(context: Any,
                          values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_roster_edit(context, values)).run()


async def prepare_roster_timing(context: Any,
                                values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError('That roster is unavailable.')
    try:
        roster_id = int(values["roster"])
    except (TypeError, ValueError):
        raise ValueError('That roster is unavailable.') from None
    roster = await workflow.get_roster(roster_id)
    if roster is None or roster.guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    options = {
        "opens_on": values.get("opens_on"),
        "closes_on": values.get("closes_on"),
        "timezone": values.get("timezone"),
        "reset_on_open": bool(values.get("reset_on_open", True)),
    }
    plan = workflow.plan_roster_timing(roster, **options)
    if plan["issue"]:
        raise ValueError(plan["issue"])
    state = await workflow.roster_edit_state(roster)
    opens_now = (not plan["clear"]
                 and plan["window"].opens_at <= plan["now"])

    def check_targets() -> None:
        if plan["clear"] or not roster.role_id:
            return
        check_role(context.guild.get_role(roster.role_id),
                   context.guild, context.guild.me, {})
        if not opens_now:
            return
        for member_id in state["member_ids"]:
            member = context.guild.get_member(member_id)
            if member is not None:
                check_member(member, context.guild.me)

    check_targets()
    if plan["clear"]:
        lines = [ACTION_ROSTER_TIMING_CLEAR.format(name=roster.name)]
    else:
        window = plan["window"]
        lines = [ACTION_ROSTER_TIMING_SET.format(
            name=roster.name,
            opens=discord.utils.format_dt(window.opens_at),
            closes=discord.utils.format_dt(window.closes_at),
        )]
        lines.append(ACTION_ROSTER_TIMING_RESET if options["reset_on_open"]
                     else ACTION_ROSTER_TIMING_KEEP)
        if opens_now and roster.role_id:
            lines.append((ACTION_ROSTER_TIMING_ROLE if options["reset_on_open"]
                          else ACTION_ROSTER_TIMING_ROLE_KEEP).format(
                role=f"<@&{roster.role_id}>",
            ))
            lines.extend(ACTION_MEMBER_LINE.format(member=f"<@{member_id}>")
                         for member_id in state["member_ids"])
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(
        channel=f"<#{channel_id}>", message_id=message_id,
    ) for channel_id, message_id in state["posts"])

    async def recheck() -> bool:
        current = await workflow.get_roster(roster_id)
        if current != roster:
            return False
        live_plan = workflow.plan_roster_timing(roster, **options)
        if live_plan["issue"] or live_plan["clear"] != plan["clear"]:
            return False
        if not plan["clear"] and live_plan["window"] != plan["window"]:
            return False
        if not plan["clear"] and (
            live_plan["window"].opens_at <= live_plan["now"]
        ) != opens_now:
            return False
        try:
            check_targets()
        except ValueError:
            return False
        return await workflow.roster_edit_state(roster) == state

    async def run() -> CommandOutcome:
        live_plan = workflow.plan_roster_timing(roster, **options)
        if live_plan["issue"] or (not plan["clear"] and (
            live_plan["window"].opens_at <= live_plan["now"]
        ) != opens_now):
            raise ValueError("Roster timing changed before execution")
        updated = await workflow.apply_roster_timing(roster, live_plan)
        actual = await workflow.get_roster(roster_id)
        if actual is None:
            raise OSError("Roster timing could not be verified")
        if plan["clear"]:
            if actual.one_off_open_ts is not None or actual.one_off_close_ts is not None:
                raise OSError("Roster timing could not be verified")
            text = f"Cleared one-off timing for **{updated.name}**."
        else:
            window = plan["window"]
            if (actual.one_off_open_ts != int(window.opens_at.timestamp())
                    or actual.one_off_close_ts != int(window.closes_at.timestamp())):
                raise OSError("Roster timing could not be verified")
            text = (f"Set **{updated.name}** to open {discord.utils.format_dt(window.opens_at)} "
                    f"and close {discord.utils.format_dt(window.closes_at)}.")
        return CommandOutcome("complete", "private", text=text,
                              after={"roster_id": roster_id,
                                     "one_off_open_ts": actual.one_off_open_ts,
                                     "one_off_close_ts": actual.one_off_close_ts})

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_TIMING_LABEL,
                      before={"roster_id": roster.id,
                              "one_off_open_ts": roster.one_off_open_ts,
                              "one_off_close_ts": roster.one_off_close_ts}),
        run,
    )


async def run_roster_timing(context: Any,
                            values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_roster_timing(context, values)).run()


async def prepare_roster_schedule(context: Any,
                                  values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError('That roster is unavailable.')
    try:
        roster_id = int(values["roster"])
    except (TypeError, ValueError):
        raise ValueError('That roster is unavailable.') from None
    roster = await workflow.get_roster(roster_id)
    if roster is None or roster.guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    options = {
        "open_day": values.get("open_day"),
        "open_time": values.get("open_time"),
        "close_day": values.get("close_day"),
        "close_time": values.get("close_time"),
        "timezone": values.get("timezone"),
        "enabled": bool(values.get("enabled", True)),
        "reset_on_open": values.get("reset_on_open"),
    }
    plan = workflow.plan_roster_schedule(roster, **options)
    if plan["issue"]:
        raise ValueError(plan["issue"])
    effect = workflow.roster_schedule_preview(roster, plan)
    state = await workflow.roster_edit_state(roster)

    def check_targets() -> None:
        if not plan["enabled"] or not roster.role_id:
            return
        check_role(context.guild.get_role(roster.role_id),
                   context.guild, context.guild.me, {})
        if not effect["starts_cycle"]:
            return
        for member_id in state["member_ids"]:
            member = context.guild.get_member(member_id)
            if member is not None:
                check_member(member, context.guild.me)

    check_targets()
    lines = [
        (ACTION_ROSTER_SCHEDULE_ENABLE if plan["enabled"]
         else ACTION_ROSTER_SCHEDULE_DISABLE).format(name=roster.name),
    ]
    for key, new in plan["changes"].items():
        old = getattr(roster, key)
        if key in ("schedule_enabled", "reset_on_open"):
            old = ACTION_VALUE_YES if old else ACTION_VALUE_NO
            new = ACTION_VALUE_YES if new else ACTION_VALUE_NO
        lines.append(ACTION_FIELD_CHANGE.format(
            field=ACTION_ROSTER_SCHEDULE_FIELDS[key],
            old=old if old is not None else ACTION_ROSTER_NO_ROLE,
            new=new if new is not None else ACTION_ROSTER_NO_ROLE,
        ))
    if plan["enabled"]:
        effective = plan["effective"]
        lines.append(ACTION_ROSTER_SCHEDULE_RULE.format(
            open_day=effective["open_day"], open_time=effective["open_time"],
            close_day=effective["close_day"], close_time=effective["close_time"],
            timezone=effective["timezone_name"],
        ))
        lines.append(ACTION_ROSTER_SCHEDULE_RESET if plan["reset_on_open"]
                     else ACTION_ROSTER_SCHEDULE_KEEP)
        if effect["window"] is not None:
            lines.append(ACTION_ROSTER_SCHEDULE_WINDOW.format(
                kind=ACTION_VALUE_CURRENT if effect["opens_now"] else ACTION_VALUE_NEXT,
                opens=discord.utils.format_dt(effect["window"].opens_at),
                closes=discord.utils.format_dt(effect["window"].closes_at),
            ))
        if effect["starts_cycle"] and roster.role_id:
            lines.append((ACTION_ROSTER_TIMING_ROLE if plan["reset_on_open"]
                          else ACTION_ROSTER_TIMING_ROLE_KEEP).format(
                role=f"<@&{roster.role_id}>",
            ))
            lines.extend(ACTION_MEMBER_LINE.format(member=f"<@{member_id}>")
                         for member_id in state["member_ids"])
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(
        channel=f"<#{channel_id}>", message_id=message_id,
    ) for channel_id, message_id in state["posts"])

    async def recheck() -> bool:
        current = await workflow.get_roster(roster_id)
        if current != roster:
            return False
        live_plan = workflow.plan_roster_schedule(roster, **options)
        if live_plan != plan:
            return False
        if workflow.roster_schedule_preview(roster, plan) != effect:
            return False
        try:
            check_targets()
        except ValueError:
            return False
        return await workflow.roster_edit_state(roster) == state

    async def run() -> CommandOutcome:
        if workflow.roster_schedule_preview(roster, plan) != effect:
            raise ValueError("Roster schedule changed before execution")
        updated, message = await workflow.apply_roster_schedule(roster, plan)
        actual = await workflow.get_roster(roster_id)
        if actual is None or actual.schedule_enabled != plan["enabled"]:
            raise OSError("Roster schedule could not be verified")
        for key, value in plan["changes"].items():
            matches = (bool(getattr(actual, key)) == bool(value)
                       if key in ("schedule_enabled", "reset_on_open")
                       else getattr(actual, key) == value)
            if not matches:
                raise OSError("Roster schedule could not be verified")
        return CommandOutcome(
            "complete", "private", text=message,
            after={"roster_id": updated.id,
                   "schedule_enabled": updated.schedule_enabled},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_SCHEDULE_LABEL,
                      before={"roster_id": roster.id,
                              "schedule_enabled": roster.schedule_enabled}),
        run,
    )


async def run_roster_schedule(context: Any,
                              values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_roster_schedule(context, values)).run()


async def prepare_roster_post(context: Any,
                              values: Mapping[str, Any]) -> PreparedCommandChange:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError('That roster is unavailable.')
    try:
        roster_id = int(values["roster"])
    except (TypeError, ValueError):
        raise ValueError('That roster is unavailable.') from None
    roster = await workflow.get_roster(roster_id)
    if roster is None or roster.guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    channel = await resolve_channel(
        context, values.get("channel") or context.source_message.channel.id,
    )
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        raise ValueError('That roster is unavailable.')
    check_post_access(channel, context.member, context.guild.me)
    state = await workflow.roster_edit_state(roster)
    render = await workflow.preview_roster_post(roster)
    effect = render["effect"]

    shown_count = 0 if effect["clears_signups"] else state["account_count"]
    lines = [
        ACTION_ROSTER_POST_LINE.format(name=roster.name, channel=channel.mention),
        ACTION_ROSTER_POST_OPEN if effect["opens"] else ACTION_ROSTER_POST_CLOSED,
        ACTION_ROSTER_POST_ACCOUNTS.format(count=shown_count),
        ACTION_ROSTER_POST_HIDDEN if roster.buttons_hidden else ACTION_ROSTER_POST_CONTROLS,
    ]
    if effect["clears_signups"]:
        lines.append(ACTION_ROSTER_POST_RESET)
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(
        channel=f"<#{channel_id}>", message_id=message_id,
    ) for channel_id, message_id in state["posts"])
    for page_number, embeds in enumerate(render["pages"], start=1):
        lines.append(ACTION_ROSTER_POST_PAGE.format(number=page_number))
        for embed in embeds:
            lines.extend(line or ACTION_PREVIEW_BLANK
                         for line in embed_text(embed).splitlines())
            for visual in (embed.image, embed.thumbnail):
                if visual.url:
                    lines.append(ACTION_ROSTER_POST_IMAGE.format(url=visual.url))

    async def recheck() -> bool:
        current = await workflow.get_roster(roster_id)
        if current != roster or workflow.roster_post_effect(roster) != effect:
            return False
        try:
            check_post_access(channel, context.member, context.guild.me)
        except ValueError:
            return False
        if await workflow.roster_edit_state(roster) != state:
            return False
        live_render = await workflow.preview_roster_post(roster)
        return live_render["signature"] == render["signature"]

    async def run() -> CommandOutcome:
        if workflow.roster_post_effect(roster) != effect:
            raise ValueError("Roster post changed before execution")
        result = await workflow.post_roster(
            roster_id, channel.send, rendered=render["rendered"],
        )
        if result is None:
            raise ValueError('That roster is unavailable.')
        opened, message = result
        if not await workflow.roster_post_registered(roster_id, message.id):
            raise OSError("Roster post could not be verified")
        return CommandOutcome(
            "complete", "public",
            result={"roster_id": roster_id, "channel_id": channel.id,
                    "message_id": message.id},
            after={"roster_id": roster_id, "status": opened.status,
                   "message_id": message.id},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_POST_LABEL,
                      before={"roster_id": roster.id, "status": roster.status}),
        run,
    )


async def run_roster_post(context: Any,
                          values: Mapping[str, Any]) -> CommandOutcome:
    return await (await prepare_roster_post(context, values)).run()


async def prepare_roster_export(context: Any,
                                values: Mapping[str, Any]) -> PreparedCommandChange | CommandOutcome:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError('That roster is unavailable.')
    try:
        roster_id = int(values["roster"])
    except (TypeError, ValueError):
        raise ValueError('That roster is unavailable.') from None
    roster = await workflow.get_roster(roster_id)
    if roster is None or roster.guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    plan = await workflow.roster_export_plan(roster)
    if not plan["accounts"]:
        return CommandOutcome(
            "complete", "private", text=f"No accounts are signed up to **{roster.name}**.",
        )
    lines = [
        ACTION_ROSTER_EXPORT_LINE.format(name=roster.name, roster_id=roster.id),
        ACTION_ROSTER_EXPORT_FILE.format(name=plan["workbook_name"]),
        ACTION_ROSTER_EXPORT_GOOGLE,
    ]
    lines.extend(ACTION_ROSTER_EXPORT_ACCOUNT.format(
        tag=tag, member=f"<@{member_id}>",
    ) for tag, member_id in plan["accounts"])

    async def recheck() -> bool:
        current = await workflow.get_roster(roster_id)
        if current != roster:
            return False
        fresh = await workflow.roster_export_plan(roster)
        return fresh["accounts"] == plan["accounts"]

    async def run() -> CommandOutcome:
        report, warning = await workflow.export_roster(
            roster, timestamp=plan["timestamp"],
        )
        if report is None:
            return CommandOutcome("complete", "private", text=warning or "")
        link, data = await workflow.deliver_roster_export(report)
        text = f"Exported **{roster.name}**."
        if link:
            text += "\n" + ACTION_ROSTER_EXPORT_LINK.format(url=link)
        elif report.google_warning:
            text += "\n" + report.google_warning
        attachments = ((AgentAttachment(report.workbook_name, data),)
                       if data is not None else ())
        return CommandOutcome(
            "complete", "private", text=text,
            attachments=attachments,
            result={"roster_id": roster_id, "workbook_name": report.workbook_name,
                    "google_link": link},
            after={"roster_id": roster_id, "workbook_name": report.workbook_name,
                   "google_link": link},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_EXPORT_LABEL,
                      before={"roster_id": roster.id,
                              "accounts": plan["accounts"]}),
        run,
    )


async def run_roster_export(context: Any,
                            values: Mapping[str, Any]) -> CommandOutcome:
    prepared = await prepare_roster_export(context, values)
    return await prepared.run() if isinstance(prepared, PreparedCommandChange) else prepared


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
        CommandAdapter("/roster timing", "confirm", run_roster_timing,
                       prepare=prepare_roster_timing,
                       action_class=ActionClass.CHANGE,
                       entity_options=(("roster", "roster"),)),
        CommandAdapter("/roster schedule", "confirm", run_roster_schedule,
                       prepare=prepare_roster_schedule,
                       action_class=ActionClass.CHANGE,
                       entity_options=(("roster", "roster"),)),
        CommandAdapter("/roster post", "confirm", run_roster_post,
                       prepare=prepare_roster_post,
                       action_class=ActionClass.CHANGE,
                       options=(ParameterInfo(
                           "channel", ACTION_ROSTER_POST_CHANNEL_OPTION,
                           False, "channel",
                       ),),
                       entity_options=(("roster", "roster"),
                                       ("channel", "discord_channel"))),
        CommandAdapter("/roster export", "private", run_roster_export,
                       entity_options=(("roster", "roster"),)),
    )
