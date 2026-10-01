"""Achievement economy and raffle command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from elbow_helper.configuration.channels import GENERAL_CHAT
from elbow_helper.features.achievements.raffle import RAFFLE_COLLECTION_CONTACT
from elbow_helper.features.agent.tools.discord_safety import (
    check_post_access, resolve_channel,
)
from elbow_helper.features.help.discovery import ParameterInfo

from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
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
    ACTION_RAFFLE_PRIZE_LABEL,
    ACTION_RAFFLE_PRIZE_LINE,
    ACTION_RAFFLE_PRIZE_UNDO_LABEL,
    ACTION_RAFFLE_PRIZE_VALUE,
    ACTION_RAFFLE_WINNERS_VALUE,
    ACTION_UNDO_CHANGED,
    ACTION_COIN_GRANT_BALANCE,
    ACTION_COIN_GRANT_LABEL,
    ACTION_COIN_GRANT_LINE,
    ACTION_REASON_LINE,
    ACTION_RAFFLE_HUB_UPDATE,
    ACTION_TICKET_GRANT_LABEL,
    ACTION_TICKET_GRANT_LINE,
    ACTION_RAFFLE_REMOVE_LINE,
    ACTION_RAFFLE_REMOVE_LABEL,
    ACTION_RAFFLE_CLEAR_LINE,
    ACTION_RAFFLE_CLEAR_TICKETS_LINE,
    ACTION_RAFFLE_CLEAR_WINNER,
    ACTION_RAFFLE_CLEAR_TICKET,
    ACTION_RAFFLE_CLEAR_NONE,
    ACTION_RAFFLE_CLEAR_LABEL,
    ACTION_RAFFLE_DRAW_LINE,
    ACTION_RAFFLE_REROLL_LINE,
    ACTION_RAFFLE_DRAW_ELIGIBLE,
    ACTION_RAFFLE_DRAW_OLD,
    ACTION_RAFFLE_DRAW_PRIOR,
    ACTION_RAFFLE_DRAW_PRIZE,
    ACTION_RAFFLE_DRAW_PINGS,
    ACTION_RAFFLE_DRAW_COLLECT,
    ACTION_RAFFLE_DRAW_HISTORY,
    ACTION_RAFFLE_DRAW_LABEL,
    ACTION_RAFFLE_REROLL_LABEL,
    ACTION_RAFFLE_WINNER_ONE,
    ACTION_RAFFLE_WINNER_MANY,
    ACTION_RAFFLE_CURRENT_MONTH,
    ACTION_RAFFLE_NO_PRIZE,
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
    workflow = _workflow(context)
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


async def prepare_raffle_remove(context: Any,
                                values: Mapping[str, Any]) -> ChangePreview:
    workflow = _workflow(context)
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
                            values: Mapping[str, Any]) -> CommandOutcome:
    workflow = _workflow(context)
    member = await _member(context, values["user"])
    if member is None:
        return CommandOutcome.unavailable()
    message = await workflow.remove_raffle_ticket(member.id)
    after = await workflow.raffle_member_ticket_state(member.id)
    if after["has_ticket"] or after["last_ticket_month"] == after["month_key"]:
        raise OSError("Ticket removal could not be verified")
    return CommandOutcome("complete", "private", text=message,
                          after={"ticket_state": after})


async def prepare_raffle_clear(context: Any,
                               values: Mapping[str, Any]) -> ChangePreview:
    workflow = _workflow(context)
    state = await workflow.raffle_clear_state()
    clear_tickets = bool(values.get("clear_tickets", False))
    lines = [ACTION_RAFFLE_CLEAR_TICKETS_LINE if clear_tickets
             else ACTION_RAFFLE_CLEAR_LINE]
    lines.extend(ACTION_RAFFLE_CLEAR_WINNER.format(member=f"<@{member_id}>")
                 for member_id in state["winners"])
    if clear_tickets:
        lines.extend(ACTION_RAFFLE_CLEAR_TICKET.format(member=f"<@{member_id}>")
                     for member_id in state["tickets"])
    if len(lines) == 1:
        lines.append(ACTION_RAFFLE_CLEAR_NONE)
    lines.append(ACTION_RAFFLE_HUB_UPDATE)

    async def recheck() -> bool:
        return await workflow.raffle_clear_state() == state

    return ChangePreview(tuple(lines), recheck, summary=ACTION_RAFFLE_CLEAR_LABEL,
                         count=max(1, len(state["winners"]) +
                                   (len(state["tickets"]) if clear_tickets else 0)),
                         before={"raffle_state": state})


async def run_raffle_clear(context: Any,
                           values: Mapping[str, Any]) -> CommandOutcome:
    workflow = _workflow(context)
    clear_tickets = bool(values.get("clear_tickets", False))
    message = await workflow.clear_raffle(clear_tickets)
    after = await workflow.raffle_clear_state()
    if after["winners"] or (clear_tickets and after["tickets"]):
        raise OSError("Raffle clear could not be verified")
    return CommandOutcome("complete", "private", text=message,
                          after={"raffle_state": after})


async def _draw_target(context: Any, values: Mapping[str, Any]):
    workflow = _workflow(context)
    channel_id = values.get("channel") or context.source_message.channel.id
    channel = await resolve_channel(context, channel_id)
    check_post_access(channel, context.member, context.guild.me)
    return workflow, channel


async def _draw_state(context: Any, values: Mapping[str, Any], *, reroll: bool):
    workflow, channel = await _draw_target(context, values)
    if reroll:
        month_key = workflow.raffle_draw_month(None)[0]
    else:
        month_key, issue, _invalid_format = workflow.raffle_draw_month(values.get("month"))
        if issue:
            raise ValueError(issue)
    state = await workflow.raffle_draw_state(context.guild.id, month_key)
    issue = workflow.raffle_draw_issue(state, reroll=reroll)
    if issue:
        raise ValueError(issue)
    return workflow, channel, state


async def _prepare_draw(context: Any, values: Mapping[str, Any], *,
                        reroll: bool) -> ChangePreview:
    workflow, channel, state = await _draw_state(context, values, reroll=reroll)
    count = state["winners_count"]
    winner_word = ACTION_RAFFLE_WINNER_ONE if count == 1 else ACTION_RAFFLE_WINNER_MANY
    if reroll:
        heading = ACTION_RAFFLE_REROLL_LINE.format(
            count=count, winner_word=winner_word, channel=channel.mention,
        )
    else:
        month = (ACTION_RAFFLE_CURRENT_MONTH if state["month_key"] == state["current_month_key"]
                 else workflow.raffle_month_label(state["month_key"]))
        heading = ACTION_RAFFLE_DRAW_LINE.format(
            count=count, winner_word=winner_word, month=month,
            channel=channel.mention,
        )
    lines = [heading, ACTION_RAFFLE_DRAW_PRIZE.format(
        prize=state["reward"] or ACTION_RAFFLE_NO_PRIZE,
    )]
    lines.extend(ACTION_RAFFLE_DRAW_OLD.format(member=f"<@{member_id}>")
                 for member_id in state["active_winners"] if reroll)
    if reroll:
        lines.extend(ACTION_RAFFLE_DRAW_PRIOR.format(member=f"<@{member_id}>")
                     for member_id in state["winners"]
                     if member_id not in state["active_winners"])
        lines.append(ACTION_RAFFLE_DRAW_HISTORY)
    lines.extend(ACTION_RAFFLE_DRAW_ELIGIBLE.format(member=f"<@{member_id}>")
                 for member_id in state["eligible"])
    lines.append(ACTION_RAFFLE_DRAW_PINGS)
    if not reroll:
        lines.append(ACTION_RAFFLE_DRAW_COLLECT.format(contact=RAFFLE_COLLECTION_CONTACT))
    lines.append(ACTION_RAFFLE_HUB_UPDATE)

    async def recheck() -> bool:
        try:
            current, target, live = await _draw_state(context, values, reroll=reroll)
        except ValueError:
            return False
        return current is workflow and target.id == channel.id and live == state

    return ChangePreview(
        tuple(lines), recheck,
        summary=(ACTION_RAFFLE_REROLL_LABEL if reroll else ACTION_RAFFLE_DRAW_LABEL),
        count=count, before={"raffle_state": state},
    )


async def prepare_raffle_draw(context: Any,
                              values: Mapping[str, Any]) -> ChangePreview:
    return await _prepare_draw(context, values, reroll=False)


async def prepare_raffle_reroll(context: Any,
                                values: Mapping[str, Any]) -> ChangePreview:
    return await _prepare_draw(context, values, reroll=True)


async def _run_draw(context: Any, values: Mapping[str, Any], *,
                    reroll: bool) -> CommandOutcome:
    workflow, channel, state = await _draw_state(context, values, reroll=reroll)
    mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
    if reroll:
        async def post(embed):
            return await channel.send(embed=embed, allowed_mentions=mentions)

        ok, _result = await workflow.reroll_raffle_winners(context.guild.id, post)
    else:
        async def post(content):
            return await channel.send(content, allowed_mentions=mentions)

        ok, _result = await workflow.draw_raffle_winners(
            context.guild.id, state["month_key"], post,
        )
    if not ok:
        return CommandOutcome.unavailable()
    after = await workflow.raffle_draw_state(context.guild.id, state["month_key"])
    if (not after["active_winners"] or
            (not reroll and len(after["winners"]) != state["winners_count"])):
        raise OSError("Raffle draw could not be verified")
    return CommandOutcome("complete", result={"channel_id": channel.id,
                                               "winners": after["active_winners"]},
                          after={"raffle_state": after})


async def run_raffle_draw(context: Any,
                          values: Mapping[str, Any]) -> CommandOutcome:
    return await _run_draw(context, values, reroll=False)


async def run_raffle_reroll(context: Any,
                            values: Mapping[str, Any]) -> CommandOutcome:
    return await _run_draw(context, values, reroll=True)


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
        CommandAdapter("/raffle clear", "confirm", run_raffle_clear,
                       prepare=prepare_raffle_clear,
                       action_class=ActionClass.IRREVERSIBLE),
        CommandAdapter("/raffle draw", "confirm", run_raffle_draw,
                       options=(ParameterInfo(
                           "channel", "Channel for the winner announcement; defaults to this channel.",
                           False, "channel",
                       ),), prepare=prepare_raffle_draw,
                       action_class=ActionClass.IRREVERSIBLE),
        CommandAdapter("/raffle reroll", "confirm", run_raffle_reroll,
                       options=(ParameterInfo(
                           "channel", "Channel for the new results; defaults to this channel.",
                           False, "channel",
                       ),), prepare=prepare_raffle_reroll,
                       action_class=ActionClass.IRREVERSIBLE),
        CommandAdapter("/raffle prize", "confirm", run_raffle_prize,
                       prepare=prepare_raffle_prize),
    )
