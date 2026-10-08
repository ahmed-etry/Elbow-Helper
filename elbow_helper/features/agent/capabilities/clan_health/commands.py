"""Clan health command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.features.agent.access import ACCESS_LEAD_PLUS, has_access_requirements
from elbow_helper.features.clan_health.ui import ClanConfigHomeView
from elbow_helper.features.clan_health.export import PreparedHealthExport
from elbow_helper.features.help.discovery import ParameterInfo

from ...models import AgentAttachment
from ...wording import ACTION_UNAVAILABLE
from ...actions.outcomes import ActionOutcome
from ...commands.registry import CommandAdapter


def _health_access(context: Any) -> bool:
    allowed = has_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD_PLUS},
    )
    if allowed:
        context.state.required_access.add(ACCESS_LEAD_PLUS)
    return allowed


def _export_outcome(export: PreparedHealthExport) -> ActionOutcome:
    if export.google_link:
        return ActionOutcome(
            "complete", text=f"{export.workbook_title}\n{export.google_link}",
        )
    return ActionOutcome(
        "complete", text="\n".join(export.summary_lines),
        attachments=(AgentAttachment(export.workbook_name, export.workbook_data or b""),),
    )


async def run_health_player(context: Any, values: Mapping[str, Any]) -> ActionOutcome:
    if not values.get("account"):
        return ActionOutcome.needs_input(("account",))
    mode = values.get("period", "last_30d")
    if mode == "custom" and (not values.get("date_from") or not values.get("date_to")):
        return ActionOutcome.needs_input(("start and end dates",))
    if not _health_access(context):
        return ActionOutcome.unavailable()
    workflow = context.bot.get_cog("ClanHealth")
    if workflow is None:
        return ActionOutcome.unavailable()
    result = await workflow.run_player_health_export(
        values["account"], mode=mode,
        date_from=values.get("date_from"), date_to=values.get("date_to"),
    )
    if result.status in {"invalid_account", "invalid_window"}:
        return ActionOutcome.needs_input(("account" if result.status == "invalid_account"
                                           else result.issue or "period",))
    if result.status == "empty":
        return ActionOutcome("empty")
    if result.status != "complete" or result.export is None:
        return ActionOutcome.unavailable()
    return _export_outcome(result.export)


async def run_health_clan(context: Any, values: Mapping[str, Any]) -> ActionOutcome:
    mode = values.get("period", "last_30d")
    if mode == "custom" and (not values.get("date_from") or not values.get("date_to")):
        return ActionOutcome.needs_input(("start and end dates",))
    if not _health_access(context):
        return ActionOutcome.unavailable()
    workflow = context.bot.get_cog("ClanHealth")
    if workflow is None:
        return ActionOutcome.unavailable()
    result = await workflow.run_clan_health_export(
        values["clan"], mode=mode,
        date_from=values.get("date_from"), date_to=values.get("date_to"),
    )
    if result.status == "invalid_window":
        return ActionOutcome.needs_input((result.issue,))
    if result.status == "invalid_clan":
        return ActionOutcome.needs_input((result.issue,))
    if result.status in {"not_configured", "empty", "unavailable"}:
        return ActionOutcome("complete", text=result.issue)
    if result.status != "complete" or result.export is None:
        return ActionOutcome.unavailable()
    return _export_outcome(result.export)


async def run_health_settings(context: Any, values: Mapping[str, Any]) -> ActionOutcome:
    if not _health_access(context):
        return ActionOutcome.unavailable()
    if context.bot.get_cog("ClanHealth") is None:
        return ActionOutcome.unavailable()

    async def open_panel(interaction):
        if not has_access_requirements(
            context.guild, context.member.id, {ACCESS_LEAD_PLUS},
        ):
            await interaction.response.send_message(ACTION_UNAVAILABLE, ephemeral=True)
            return
        await ClanConfigHomeView.open(interaction, values["clan"])

    return ActionOutcome("complete", "private", private_panel=open_panel)


def health_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/health player", "public", run_health_player, (
        ParameterInfo("date_from", "Start date for Custom dates.", False, "string"),
        ParameterInfo("date_to", "End date for Custom dates.", False, "string"),
    ), entity_options=(("account", "clash_account"),)),
        CommandAdapter("/health clan", "public", run_health_clan, (
            ParameterInfo("date_from", "Start date for Custom dates.", False, "string"),
            ParameterInfo("date_to", "End date for Custom dates.", False, "string"),
        )),
        CommandAdapter("/health settings", "private", run_health_settings))
