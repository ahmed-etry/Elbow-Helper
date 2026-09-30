"""Recruitment command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..outcomes import CommandOutcome
from ..registry import CommandAdapter


async def run_opinion(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    ticket = str(values.get("ticket") or "").strip()
    if not ticket:
        return CommandOutcome.needs_input(("ticket",))
    workflow = context.bot.get_cog("Recruitment")
    if workflow is None:
        return CommandOutcome.unavailable()
    channel = workflow.resolve_opinion_ticket(context.guild, context.member, ticket)
    if channel is None:
        return CommandOutcome.needs_input(("ticket",))
    parts = await workflow._build_ticket_second_opinion(channel)
    if not parts:
        return CommandOutcome("empty", "private")
    return CommandOutcome("complete", "private", private_parts=tuple(parts))


def recruitment_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/opinion", "private", run_opinion,
                           entity_options=(("ticket", "discord_channel"),)),)
