"""Clan health command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from typing import Any

from elbow_helper.features.agent.access import ACCESS_LEAD_PLUS, has_access_requirements
from elbow_helper.features.clan_health.ui import ClanConfigHomeView
from elbow_helper.features.clan_health.export import PreparedHealthExport
from elbow_helper.features.help.discovery import ParameterInfo

from ...models import AgentAttachment
from ...wording import COMMAND_UNAVAILABLE
from ..outcomes import CommandOutcome
from ..registry import CommandAdapter


def _health_period_issue(plan: Mapping[str, Any], values: Mapping[str, Any]) -> str:
    selected = values.get("period", "last_30d")
    if selected != "custom":
        return "Use no declared period with a preset command period." if plan["periods"] else ""
    if not values.get("date_from") or not values.get("date_to"):
        return ""
    try:
        start = datetime.combine(date.fromisoformat(values["date_from"]), time.min, timezone.utc)
        end = datetime.combine(date.fromisoformat(values["date_to"]), time.min, timezone.utc)
        ranges = [
            (datetime.fromisoformat(period["start"].replace("Z", "+00:00")),
             datetime.fromisoformat(period["end"].replace("Z", "+00:00")))
            for period in plan["periods"] if period["kind"] == "utc_range"
        ]
    except (TypeError, ValueError, KeyError):
        return "Use UTC dates for the custom command period."
    if start >= end or not any(lower <= start and end <= upper for lower, upper in ranges):
        return "Keep the custom command period inside the declared UTC range."
    return ""


def _health_access(context: Any) -> bool:
    allowed = has_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD_PLUS},
    )
    if allowed:
        context.state.required_access.add(ACCESS_LEAD_PLUS)
    return allowed


def _export_outcome(export: PreparedHealthExport) -> CommandOutcome:
    if export.google_link:
        return CommandOutcome(
            "complete", text=f"{export.workbook_title}\n{export.google_link}",
        )
    return CommandOutcome(
        "complete", text="\n".join(export.summary_lines),
        attachments=(AgentAttachment(export.workbook_name, export.workbook_data or b""),),
    )


async def run_health_player(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    if not values.get("account"):
        return CommandOutcome.needs_input(("account",))
    mode = values.get("period", "last_30d")
    if mode == "custom" and (not values.get("date_from") or not values.get("date_to")):
        return CommandOutcome.needs_input(("start and end dates",))
    if not _health_access(context):
        return CommandOutcome.unavailable()
    workflow = context.bot.get_cog("ClanHealth")
    if workflow is None:
        return CommandOutcome.unavailable()
    result = await workflow.run_player_health_export(
        values["account"], mode=mode,
        date_from=values.get("date_from"), date_to=values.get("date_to"),
    )
    if result.status in {"invalid_account", "invalid_window"}:
        return CommandOutcome.needs_input(("account" if result.status == "invalid_account"
                                           else result.issue or "period",))
    if result.status == "empty":
        return CommandOutcome("empty")
    if result.status != "complete" or result.export is None:
        return CommandOutcome.unavailable()
    return _export_outcome(result.export)


async def run_health_clan(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    mode = values.get("period", "last_30d")
    if mode == "custom" and (not values.get("date_from") or not values.get("date_to")):
        return CommandOutcome.needs_input(("start and end dates",))
    if not _health_access(context):
        return CommandOutcome.unavailable()
    workflow = context.bot.get_cog("ClanHealth")
    if workflow is None:
        return CommandOutcome.unavailable()
    result = await workflow.run_clan_health_export(
        values["clan"], mode=mode,
        date_from=values.get("date_from"), date_to=values.get("date_to"),
    )
    if result.status == "invalid_window":
        return CommandOutcome.needs_input((result.issue,))
    if result.status == "invalid_clan":
        return CommandOutcome.needs_input((result.issue,))
    if result.status in {"not_configured", "empty", "unavailable"}:
        return CommandOutcome("complete", text=result.issue)
    if result.status != "complete" or result.export is None:
        return CommandOutcome.unavailable()
    return _export_outcome(result.export)


async def run_health_settings(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    if not _health_access(context):
        return CommandOutcome.unavailable()
    if context.bot.get_cog("ClanHealth") is None:
        return CommandOutcome.unavailable()

    async def open_panel(interaction):
        if not has_access_requirements(
            context.guild, context.member.id, {ACCESS_LEAD_PLUS},
        ):
            await interaction.response.send_message(COMMAND_UNAVAILABLE, ephemeral=True)
            return
        await ClanConfigHomeView.open(interaction, values["clan"])

    return CommandOutcome("complete", "private", private_panel=open_panel)


def health_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/health player", "public", run_health_player, (
        ParameterInfo("date_from", "Start date for Custom dates.", False, "string"),
        ParameterInfo("date_to", "End date for Custom dates.", False, "string"),
    ), entity_options=(("account", "clash_account"),),
        check_period=_health_period_issue),
        CommandAdapter("/health clan", "public", run_health_clan, (
            ParameterInfo("date_from", "Start date for Custom dates.", False, "string"),
            ParameterInfo("date_to", "End date for Custom dates.", False, "string"),
        ), check_period=_health_period_issue),
        CommandAdapter("/health settings", "private", run_health_settings))
