"""Achievements commands."""

from __future__ import annotations
from collections.abc import Mapping
from typing import Any
import discord
from elbow_helper.configuration.channels import GENERAL_CHAT
from ...actions.contracts import ActionClass, ChangePreview
from ...wording import (
    ACTION_ACHIEVEMENT_ANNOUNCE_LINE,
    ACTION_ACHIEVEMENT_AWARD_LABEL,
    ACTION_ACHIEVEMENT_AWARD_LINE,
    ACTION_ACHIEVEMENT_COIN_LINE,
    ACTION_ACHIEVEMENT_COIN_ADD,
    ACTION_ACHIEVEMENT_COIN_REMOVE,
    ACTION_ACHIEVEMENT_COIN_PLURAL,
    ACTION_ACHIEVEMENT_COIN_SINGULAR,
    ACTION_ACHIEVEMENT_REMOVE_LABEL,
    ACTION_ACHIEVEMENT_REMOVE_LINE,
    ACTION_COIN_GRANT_BALANCE,
    ACTION_COIN_GRANT_LABEL,
    ACTION_COIN_GRANT_LINE,
    ACTION_REASON_LINE,
    ACTION_RAFFLE_HUB_UPDATE,
    ACTION_TICKET_GRANT_LABEL,
    ACTION_TICKET_GRANT_LINE,
    ACTION_RAFFLE_REMOVE_LINE,
    ACTION_RAFFLE_REMOVE_LABEL,
)
from ...actions.outcomes import ActionOutcome
from ...commands.registry import CommandAdapter


def achievement_workflow(context: Any):
    workflow = context.bot.get_cog("Achievements")
    if workflow is None:
        raise ValueError("The raffle is unavailable")
    return workflow


async def _member(context: Any, member_id: int):
    member = context.guild.get_member(member_id)
    if member is not None:
        return member
    try:
        return await context.guild.fetch_member(member_id)
    except discord.DiscordException:
        return None


async def _achievement_state(workflow: Any, member_id: int, query: str):
    try:
        state = await workflow.achievement_change_state(member_id, query)
    except ValueError as error:
        raise ValueError('More than one achievement matches that name.') from error
    if state is None:
        raise ValueError('No achievement matches that name.')
    return state


def _coin_line(action: str, count: int) -> str:
    return ACTION_ACHIEVEMENT_COIN_LINE.format(
        action=action, count=count,
        coin_word=(ACTION_ACHIEVEMENT_COIN_SINGULAR if count == 1
                   else ACTION_ACHIEVEMENT_COIN_PLURAL),
    )


async def prepare_achievement_award(context: Any,
                                    values: Mapping[str, Any]) -> ChangePreview:
    workflow = achievement_workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        raise ValueError('That member is unavailable.')
    state = await _achievement_state(workflow, member.id, values["achievement"])
    if state["completed_date"] is not None:
        raise ValueError('That member already has that achievement.')
    silent = bool(values.get("silent", False))
    lines = [ACTION_ACHIEVEMENT_AWARD_LINE.format(
        achievement=state["name"], member=member.mention,
    )]
    if state["reward"]:
        lines.append(_coin_line(ACTION_ACHIEVEMENT_COIN_ADD, state["reward"]))
    announce = not silent and workflow.bot.get_channel(GENERAL_CHAT) is not None
    if announce:
        lines.append(ACTION_ACHIEVEMENT_ANNOUNCE_LINE.format(
            channel=f"<#{GENERAL_CHAT}>",
        ))

    async def recheck() -> bool:
        if await _member(context, member.id) is None:
            return False
        return (await workflow.achievement_change_state(member.id, state["id"]) == state
                and (not silent and workflow.bot.get_channel(GENERAL_CHAT) is not None)
                == announce)

    return ChangePreview(tuple(lines), recheck, summary=ACTION_ACHIEVEMENT_AWARD_LABEL,
                         before={"achievement": state})


async def run_achievement_award(context: Any,
                                values: Mapping[str, Any]) -> ActionOutcome:
    workflow = achievement_workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        return ActionOutcome.unavailable()
    state = await _achievement_state(workflow, member.id, values["achievement"])
    success, message = await workflow.manually_award_achievement(
        member.id, state["id"], context.member.display_name,
        bool(values.get("silent", False)),
    )
    if not success:
        return ActionOutcome.unavailable()
    after = await workflow.achievement_change_state(member.id, state["id"])
    if after is None or after["completed_date"] is None:
        raise OSError("Achievement award could not be verified")
    return ActionOutcome("complete", "private", text=f"{message} to {member.display_name}.",
                          after={"achievement": after})


async def prepare_achievement_remove(context: Any,
                                     values: Mapping[str, Any]) -> ChangePreview:
    workflow = achievement_workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        raise ValueError('That member is unavailable.')
    state = await _achievement_state(workflow, member.id, values["achievement"])
    if state["completed_date"] is None:
        raise ValueError('That member does not have that achievement.')
    lines = [ACTION_ACHIEVEMENT_REMOVE_LINE.format(
        achievement=state["name"], member=member.mention,
    )]
    if state["reward"] and state["reversal_due"]:
        lines.append(_coin_line(ACTION_ACHIEVEMENT_COIN_REMOVE, state["reward"]))

    async def recheck() -> bool:
        if await _member(context, member.id) is None:
            return False
        return await workflow.achievement_change_state(member.id, state["id"]) == state

    return ChangePreview(tuple(lines), recheck, summary=ACTION_ACHIEVEMENT_REMOVE_LABEL,
                         before={"achievement": state})


async def run_achievement_remove(context: Any,
                                 values: Mapping[str, Any]) -> ActionOutcome:
    workflow = achievement_workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        return ActionOutcome.unavailable()
    state = await _achievement_state(workflow, member.id, values["achievement"])
    success, message = await workflow.manually_remove_achievement(
        member.id, state["id"], context.member.display_name,
    )
    if not success:
        return ActionOutcome.unavailable()
    after = await workflow.achievement_change_state(member.id, state["id"])
    if after is None or after["completed_date"] is not None:
        raise OSError("Achievement removal could not be verified")
    return ActionOutcome("complete", "private", text=f"{message} from {member.display_name}.",
                          after={"achievement": after})


async def prepare_grant_coins(context: Any,
                              values: Mapping[str, Any]) -> ChangePreview:
    workflow = achievement_workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        raise ValueError('That member is unavailable.')
    category = values["category"]
    amount = max(1, min(int(values["amount"]), 10))
    state = await workflow.manual_coin_grant_state(
        member, category, amount, context.member,
    )
    if state["issue"]:
        raise ValueError(state["issue"])

    async def recheck() -> bool:
        if await _member(context, member.id) is None:
            return False
        return await workflow.manual_coin_grant_state(
            member, category, amount, context.member,
        ) == state

    return ChangePreview((
        ACTION_COIN_GRANT_LINE.format(
            count=amount,
            coin_word=(ACTION_ACHIEVEMENT_COIN_SINGULAR if amount == 1
                       else ACTION_ACHIEVEMENT_COIN_PLURAL),
            member=member.mention, category=category,
        ),
        ACTION_REASON_LINE.format(reason=values["reason"]),
        ACTION_COIN_GRANT_BALANCE.format(
            old=state["balance"], new=state["balance"] + amount,
        ),
    ), recheck, summary=ACTION_COIN_GRANT_LABEL, before={"coin_state": state})


async def run_grant_coins(context: Any,
                          values: Mapping[str, Any]) -> ActionOutcome:
    workflow = achievement_workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        return ActionOutcome.unavailable()
    category = values["category"]
    amount = max(1, min(int(values["amount"]), 10))
    before = await workflow.manual_coin_grant_state(
        member, category, amount, context.member,
    )
    ok, message = await workflow.grant_coins_to_member(
        member, category, amount, values["reason"], context.member,
    )
    if not ok:
        return ActionOutcome.unavailable()
    after = await workflow.manual_coin_grant_state(
        member, category, amount, context.member,
    )
    if after["balance"] != before["balance"] + amount:
        raise OSError("Coin grant could not be verified")
    return ActionOutcome("complete", "private", text=message,
                          after={"coin_state": after})


async def prepare_grant_ticket(context: Any,
                               values: Mapping[str, Any]) -> ChangePreview:
    workflow = achievement_workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        raise ValueError('That member is unavailable.')
    state = await workflow.ticket_grant_state(member.id)
    if state["issue"]:
        raise ValueError(state["issue"])

    async def recheck() -> bool:
        if await _member(context, member.id) is None:
            return False
        return await workflow.ticket_grant_state(member.id) == state

    return ChangePreview((
        ACTION_TICKET_GRANT_LINE.format(member=member.mention),
        ACTION_REASON_LINE.format(reason=values["reason"]),
        ACTION_RAFFLE_HUB_UPDATE,
    ), recheck, summary=ACTION_TICKET_GRANT_LABEL,
        before={"ticket_state": state})


async def run_grant_ticket(context: Any,
                           values: Mapping[str, Any]) -> ActionOutcome:
    workflow = achievement_workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        return ActionOutcome.unavailable()
    ok, message = await workflow.grant_raffle_ticket(member.id, values["reason"])
    if not ok:
        return ActionOutcome.unavailable()
    after = await workflow.ticket_grant_state(member.id)
    if not after["has_ticket"]:
        raise OSError("Ticket grant could not be verified")
    return ActionOutcome("complete", "private", text=message,
                          after={"ticket_state": after})


async def prepare_raffle_remove(context: Any,
                                values: Mapping[str, Any]) -> ChangePreview:
    workflow = achievement_workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        raise ValueError('That member is unavailable.')
    state = await workflow.raffle_member_ticket_state(member.id)
    if not state["has_ticket"]:
        raise ValueError('That member has no raffle ticket this month.')

    async def recheck() -> bool:
        if await _member(context, member.id) is None:
            return False
        return await workflow.raffle_member_ticket_state(member.id) == state

    return ChangePreview((
        ACTION_RAFFLE_REMOVE_LINE.format(member=member.mention),
    ), recheck, summary=ACTION_RAFFLE_REMOVE_LABEL,
        before={"ticket_state": state})


async def run_raffle_remove(context: Any,
                            values: Mapping[str, Any]) -> ActionOutcome:
    workflow = achievement_workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        return ActionOutcome.unavailable()
    message = await workflow.remove_raffle_ticket(member.id)
    after = await workflow.raffle_member_ticket_state(member.id)
    if after["has_ticket"] or after["last_ticket_month"] == after["month_key"]:
        raise OSError("Ticket removal could not be verified")
    return ActionOutcome("complete", "private", text=message,
                          after={"ticket_state": after})


def achievement_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter("/achievement award", "confirm", run_achievement_award,
                       prepare=prepare_achievement_award,
                       action_class=ActionClass.CHANGE),
        CommandAdapter("/achievement remove", "confirm", run_achievement_remove,
                       prepare=prepare_achievement_remove,
                       action_class=ActionClass.CHANGE),
        CommandAdapter("/grant coins", "confirm", run_grant_coins,
                       prepare=prepare_grant_coins,
                       action_class=ActionClass.CHANGE),
        CommandAdapter("/grant ticket", "confirm", run_grant_ticket,
                       prepare=prepare_grant_ticket,
                       action_class=ActionClass.CHANGE),
        CommandAdapter("/raffle remove", "confirm", run_raffle_remove,
                       prepare=prepare_raffle_remove,
                       action_class=ActionClass.CHANGE),
    )
