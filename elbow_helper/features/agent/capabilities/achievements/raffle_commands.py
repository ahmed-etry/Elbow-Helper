"""Achievements raffle commands."""

from __future__ import annotations
from collections.abc import Mapping
from typing import Any
import discord
from elbow_helper.features.achievements.raffle import RAFFLE_COLLECTION_CONTACT
from elbow_helper.features.agent.discord_actions.safety import check_post_access, resolve_channel
from elbow_helper.features.help.discovery import ParameterInfo
from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...wording import (
    ACTION_RAFFLE_PRIZE_LABEL,
    ACTION_RAFFLE_PRIZE_LINE,
    ACTION_RAFFLE_PRIZE_UNDO_LABEL,
    ACTION_RAFFLE_PRIZE_VALUE,
    ACTION_RAFFLE_WINNERS_VALUE,
    ACTION_UNDO_CHANGED,
    ACTION_RAFFLE_HUB_UPDATE,
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
from ...actions.outcomes import CommandOutcome
from ...commands.registry import CommandAdapter
from .commands import _workflow


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


def raffle_adapters() -> tuple[CommandAdapter, ...]:
    return (
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
