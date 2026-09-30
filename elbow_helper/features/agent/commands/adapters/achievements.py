"""Achievement economy and raffle command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...actions.contracts import ChangePreview, PreparedAction
from ...wording import (
    ACTION_RAFFLE_PRIZE_LABEL, ACTION_RAFFLE_PRIZE_LINE,
    ACTION_RAFFLE_PRIZE_UNDO_LABEL, ACTION_RAFFLE_PRIZE_VALUE,
    ACTION_RAFFLE_WINNERS_VALUE, ACTION_UNDO_CHANGED,
)
from ..outcomes import CommandOutcome
from ..registry import CommandAdapter


def _workflow(context: Any):
    workflow = context.bot.get_cog("Achievements")
    if workflow is None:
        raise ValueError("The raffle is unavailable")
    return workflow


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
    return (CommandAdapter("/raffle prize", "confirm", run_raffle_prize,
                           prepare=prepare_raffle_prize),)
