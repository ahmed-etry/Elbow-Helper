"""Achievement economy and raffle command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.configuration.channels import GENERAL_CHAT

from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...wording import (
    ACTION_ACHIEVEMENT_ALREADY_HELD, ACTION_ACHIEVEMENT_AMBIGUOUS,
    ACTION_ACHIEVEMENT_ANNOUNCE_LINE, ACTION_ACHIEVEMENT_AWARD_LABEL,
    ACTION_ACHIEVEMENT_AWARD_LINE, ACTION_ACHIEVEMENT_COIN_LINE,
    ACTION_ACHIEVEMENT_COIN_ADD, ACTION_ACHIEVEMENT_COIN_REMOVE,
    ACTION_ACHIEVEMENT_COIN_PLURAL, ACTION_ACHIEVEMENT_COIN_SINGULAR,
    ACTION_ACHIEVEMENT_MEMBER_UNAVAILABLE, ACTION_ACHIEVEMENT_NOT_HELD,
    ACTION_ACHIEVEMENT_REMOVE_LABEL,
    ACTION_ACHIEVEMENT_REMOVE_LINE, ACTION_ACHIEVEMENT_UNKNOWN,
    ACTION_RAFFLE_PRIZE_LABEL, ACTION_RAFFLE_PRIZE_LINE,
    ACTION_RAFFLE_PRIZE_UNDO_LABEL, ACTION_RAFFLE_PRIZE_VALUE,
    ACTION_RAFFLE_WINNERS_VALUE, ACTION_UNDO_CHANGED,
    ACTION_COIN_GRANT_BALANCE, ACTION_COIN_GRANT_LABEL,
    ACTION_COIN_GRANT_LINE, ACTION_COIN_GRANT_REASON,
    ACTION_TICKET_GRANT_HUB, ACTION_TICKET_GRANT_LABEL,
    ACTION_TICKET_GRANT_LINE, ACTION_TICKET_GRANT_REASON,
)
from ..outcomes import CommandOutcome
from ..registry import CommandAdapter


def _workflow(context: Any):
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
        raise ValueError(ACTION_ACHIEVEMENT_AMBIGUOUS) from error
    if state is None:
        raise ValueError(ACTION_ACHIEVEMENT_UNKNOWN)
    return state


def _coin_line(action: str, count: int) -> str:
    return ACTION_ACHIEVEMENT_COIN_LINE.format(
        action=action, count=count,
        coin_word=(ACTION_ACHIEVEMENT_COIN_SINGULAR if count == 1
                   else ACTION_ACHIEVEMENT_COIN_PLURAL),
    )


async def prepare_achievement_award(context: Any,
                                    values: Mapping[str, Any]) -> ChangePreview:
    workflow = _workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        raise ValueError(ACTION_ACHIEVEMENT_MEMBER_UNAVAILABLE)
    state = await _achievement_state(workflow, member.id, values["achievement"])
    if state["completed_date"] is not None:
        raise ValueError(ACTION_ACHIEVEMENT_ALREADY_HELD)
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
                                values: Mapping[str, Any]) -> CommandOutcome:
    workflow = _workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        return CommandOutcome.unavailable()
    state = await _achievement_state(workflow, member.id, values["achievement"])
    success, message = await workflow.manually_award_achievement(
        member.id, state["id"], context.member.display_name,
        bool(values.get("silent", False)),
    )
    if not success:
        return CommandOutcome.unavailable()
    after = await workflow.achievement_change_state(member.id, state["id"])
    if after is None or after["completed_date"] is None:
        raise OSError("Achievement award could not be verified")
    return CommandOutcome("complete", "private", text=f"{message} to {member.display_name}.",
                          after={"achievement": after})


async def prepare_achievement_remove(context: Any,
                                     values: Mapping[str, Any]) -> ChangePreview:
    workflow = _workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        raise ValueError(ACTION_ACHIEVEMENT_MEMBER_UNAVAILABLE)
    state = await _achievement_state(workflow, member.id, values["achievement"])
    if state["completed_date"] is None:
        raise ValueError(ACTION_ACHIEVEMENT_NOT_HELD)
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
                                 values: Mapping[str, Any]) -> CommandOutcome:
    workflow = _workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        return CommandOutcome.unavailable()
    state = await _achievement_state(workflow, member.id, values["achievement"])
    success, message = await workflow.manually_remove_achievement(
        member.id, state["id"], context.member.display_name,
    )
    if not success:
        return CommandOutcome.unavailable()
    after = await workflow.achievement_change_state(member.id, state["id"])
    if after is None or after["completed_date"] is not None:
        raise OSError("Achievement removal could not be verified")
    return CommandOutcome("complete", "private", text=f"{message} from {member.display_name}.",
                          after={"achievement": after})


async def prepare_grant_coins(context: Any,
                              values: Mapping[str, Any]) -> ChangePreview:
    workflow = _workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        raise ValueError(ACTION_ACHIEVEMENT_MEMBER_UNAVAILABLE)
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
        ACTION_COIN_GRANT_REASON.format(reason=values["reason"]),
        ACTION_COIN_GRANT_BALANCE.format(
            old=state["balance"], new=state["balance"] + amount,
        ),
    ), recheck, summary=ACTION_COIN_GRANT_LABEL, before={"coin_state": state})


async def run_grant_coins(context: Any,
                          values: Mapping[str, Any]) -> CommandOutcome:
    workflow = _workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        return CommandOutcome.unavailable()
    category = values["category"]
    amount = max(1, min(int(values["amount"]), 10))
    before = await workflow.manual_coin_grant_state(
        member, category, amount, context.member,
    )
    ok, message = await workflow.grant_coins_to_member(
        member, category, amount, values["reason"], context.member,
    )
    if not ok:
        return CommandOutcome.unavailable()
    after = await workflow.manual_coin_grant_state(
        member, category, amount, context.member,
    )
    if after["balance"] != before["balance"] + amount:
        raise OSError("Coin grant could not be verified")
    return CommandOutcome("complete", "private", text=message,
                          after={"coin_state": after})


async def prepare_grant_ticket(context: Any,
                               values: Mapping[str, Any]) -> ChangePreview:
    workflow = _workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        raise ValueError(ACTION_ACHIEVEMENT_MEMBER_UNAVAILABLE)
    state = await workflow.ticket_grant_state(member.id)
    if state["issue"]:
        raise ValueError(state["issue"])

    async def recheck() -> bool:
        if await _member(context, member.id) is None:
            return False
        return await workflow.ticket_grant_state(member.id) == state

    return ChangePreview((
        ACTION_TICKET_GRANT_LINE.format(member=member.mention),
        ACTION_TICKET_GRANT_REASON.format(reason=values["reason"]),
        ACTION_TICKET_GRANT_HUB,
    ), recheck, summary=ACTION_TICKET_GRANT_LABEL,
        before={"ticket_state": state})


async def run_grant_ticket(context: Any,
                           values: Mapping[str, Any]) -> CommandOutcome:
    workflow = _workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        return CommandOutcome.unavailable()
    ok, message = await workflow.grant_raffle_ticket(member.id, values["reason"])
    if not ok:
        return CommandOutcome.unavailable()
    after = await workflow.ticket_grant_state(member.id)
    if not after["has_ticket"]:
        raise OSError("Ticket grant could not be verified")
    return CommandOutcome("complete", "private", text=message,
                          after={"ticket_state": after})


def _lines(before: tuple[int, str | None, str | None],
           prize: str, winners: int) -> tuple[str, ...]:
    _, old_prize, old_winners = before
    return (
        ACTION_RAFFLE_PRIZE_LINE,
        ACTION_RAFFLE_PRIZE_VALUE.format(old=old_prize or "Prize not set", new=prize),
        ACTION_RAFFLE_WINNERS_VALUE.format(old=old_winners or "1", new=winners),
    )


async def prepare_raffle_prize(context: Any,
                               values: Mapping[str, Any]) -> ChangePreview:
    prize = values["prize"].strip()
    if not prize:
        raise ValueError("Enter a raffle prize.")
    winners = int(values.get("winners", 1))
    if winners < 1:
        raise ValueError("Choose at least one winner")
    workflow = _workflow(context)
    before = await workflow.raffle_prize_state()

    async def recheck() -> bool:
        return await workflow.raffle_prize_state() == before

    return ChangePreview(
        _lines(before, prize, winners), recheck,
        summary=ACTION_RAFFLE_PRIZE_LABEL,
        before={"month_key": before[0], "prize": before[1], "winners": before[2]},
    )


async def run_raffle_prize(context: Any,
                           values: Mapping[str, Any]) -> CommandOutcome:
    workflow = _workflow(context)
    prize = values["prize"].strip()
    winners = int(values.get("winners", 1))
    message = await workflow.apply_raffle_prize(prize, winners)
    month_key, saved_prize, saved_winners = await workflow.raffle_prize_state()
    if saved_prize != prize or saved_winners != str(winners):
        raise OSError("Raffle prize change could not be verified")
    return CommandOutcome(
        "complete", "private", text=message,
        after={"month_key": month_key, "prize": saved_prize,
               "winners": saved_winners},
    )


async def prepare_raffle_prize_undo(context: Any,
                                    log: Mapping[str, Any]) -> PreparedAction:
    before = log.get("before")
    after = log.get("after")
    if before is None or after is None:
        raise ValueError("That raffle prize change is unavailable")
    workflow = _workflow(context)
    current = await workflow.raffle_prize_state()
    expected = (after["month_key"], after["prize"], after["winners"])

    async def recheck() -> bool:
        return await workflow.raffle_prize_state() == expected

    async def run() -> CommandOutcome:
        await workflow.restore_raffle_prize(
            before["month_key"], before["prize"], before["winners"],
        )
        restored = await workflow.raffle_prize_state()
        if restored != (before["month_key"], before["prize"], before["winners"]):
            raise OSError("Raffle prize undo could not be verified")
        return CommandOutcome("complete", after={"restored": True})

    return PreparedAction(
        "undo_raffle_prize", {"month_key": before["month_key"]},
        ChangePreview((
            *_lines(current, before["prize"] or "Prize not set",
                    int(before["winners"] or 1)),
            *((ACTION_UNDO_CHANGED,) if current != expected else ()),
        ), recheck, summary=ACTION_RAFFLE_PRIZE_UNDO_LABEL,
            before={"month_key": current[0], "prize": current[1],
                    "winners": current[2]}),
        run,
    )


def achievement_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter("/achievement award", "confirm", run_achievement_award,
                       prepare=prepare_achievement_award,
                       action_class=ActionClass.IRREVERSIBLE),
        CommandAdapter("/achievement remove", "confirm", run_achievement_remove,
                       prepare=prepare_achievement_remove,
                       action_class=ActionClass.IRREVERSIBLE),
        CommandAdapter("/grant coins", "confirm", run_grant_coins,
                       prepare=prepare_grant_coins,
                       action_class=ActionClass.IRREVERSIBLE),
        CommandAdapter("/grant ticket", "confirm", run_grant_ticket,
                       prepare=prepare_grant_ticket,
                       action_class=ActionClass.IRREVERSIBLE),
        CommandAdapter("/raffle prize", "confirm", run_raffle_prize,
                       prepare=prepare_raffle_prize),
    )
