"""Event tracker command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.features.agent.access import ACCESS_LEAD, has_access_requirements
from elbow_helper.features.event_stats.commands import open_event_panel

from ...actions.contracts import ChangePreview
from ...wording import ACTION_UNAVAILABLE
from ...wording import (
    ACTION_EVENT_UPDATE_LABEL, ACTION_EVENT_UPDATE_LINE,
    ACTION_EVENT_UPDATE_TARGET,
)
from ...actions.outcomes import ActionOutcome
from ...commands.registry import CommandAdapter


async def run_event_panel(context: Any, values: Mapping[str, Any]) -> ActionOutcome:
    del values
    if not has_access_requirements(context.guild, context.member.id, {ACCESS_LEAD}):
        return ActionOutcome.unavailable()
    workflow = context.bot.get_cog("EventStatsCog")
    if workflow is None:
        return ActionOutcome.unavailable()
    context.state.required_access.add(ACCESS_LEAD)

    async def open_panel(interaction):
        if not has_access_requirements(context.guild, context.member.id, {ACCESS_LEAD}):
            await interaction.response.send_message(ACTION_UNAVAILABLE, ephemeral=True)
            return
        await open_event_panel(interaction, workflow, context.guild)

    return ActionOutcome("complete", "private", private_panel=open_panel)


def event_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter("/event panel", "private", run_event_panel),
        CommandAdapter("/event update", "confirm", run_event_update,
                       prepare=prepare_event_update),
    )


async def prepare_event_update(context: Any, values: Mapping[str, Any]) -> ChangePreview:
    del values
    if not has_access_requirements(context.guild, context.member.id, {ACCESS_LEAD}):
        raise ValueError("Event stats are unavailable")
    workflow = context.bot.get_cog("EventStatsCog")
    if workflow is None:
        raise ValueError("Event stats are unavailable")
    context.state.required_access.add(ACCESS_LEAD)
    snapshot = workflow.queries.snapshot(context.guild)
    targets = tuple((row.event_key, row.name) for row in snapshot.rows)

    async def recheck() -> bool:
        if not has_access_requirements(context.guild, context.member.id, {ACCESS_LEAD}):
            return False
        current = workflow.queries.snapshot(context.guild)
        return tuple((row.event_key, row.name) for row in current.rows) == targets

    return ChangePreview((
        ACTION_EVENT_UPDATE_LINE,
        *(ACTION_EVENT_UPDATE_TARGET.format(name=name) for _, name in targets),
    ), recheck, summary=ACTION_EVENT_UPDATE_LABEL)


async def run_event_update(context: Any, values: Mapping[str, Any]) -> ActionOutcome:
    del values
    if not has_access_requirements(context.guild, context.member.id, {ACCESS_LEAD}):
        return ActionOutcome.unavailable()
    workflow = context.bot.get_cog("EventStatsCog")
    if workflow is None:
        return ActionOutcome.unavailable()
    await workflow.force_refresh(context.guild)
    return ActionOutcome("complete")
