"""Rosters timing commands."""

from __future__ import annotations
from collections.abc import Mapping
from typing import Any
import discord
from elbow_helper.features.agent.discord_actions.safety import check_member, check_role
from ...actions.contracts import ActionClass, ChangePreview
from ...wording import (
    ACTION_ROSTER_NO_ROLE,
    ACTION_VALUE_YES,
    ACTION_VALUE_NO,
    ACTION_MEMBER_LINE,
    ACTION_FIELD_CHANGE,
    ACTION_ROSTER_POST_REFRESH,
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
)
from ...actions.outcomes import CommandOutcome
from ...commands.registry import CommandAdapter, PreparedCommandChange


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


def timing_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter("/roster timing", "confirm", run_roster_timing,
                       prepare=prepare_roster_timing,
                       action_class=ActionClass.CHANGE,
                       entity_options=(("roster", "roster"),)),
        CommandAdapter("/roster schedule", "confirm", run_roster_schedule,
                       prepare=prepare_roster_schedule,
                       action_class=ActionClass.CHANGE,
                       entity_options=(("roster", "roster"),)),
    )
