"""Event tracker command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.features.agent.access import ACCESS_LEAD, has_access_requirements
from elbow_helper.features.event_stats.commands import open_event_panel

from ...wording import COMMAND_UNAVAILABLE
from ..outcomes import CommandOutcome
from ..registry import CommandAdapter


async def run_event_panel(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    del values
    if not has_access_requirements(context.guild, context.member.id, {ACCESS_LEAD}):
        return CommandOutcome.unavailable()
    workflow = context.bot.get_cog("EventStatsCog")
    if workflow is None:
        return CommandOutcome.unavailable()
    context.state.required_access.add(ACCESS_LEAD)

    async def open_panel(interaction):
        if not has_access_requirements(context.guild, context.member.id, {ACCESS_LEAD}):
            await interaction.response.send_message(COMMAND_UNAVAILABLE, ephemeral=True)
            return
        await open_event_panel(interaction, workflow, context.guild)

    return CommandOutcome("complete", "private", private_panel=open_panel)


def event_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/event panel", "private", run_event_panel),)
