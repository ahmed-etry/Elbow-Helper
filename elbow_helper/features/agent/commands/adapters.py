"""Feature adapters for enabled agent commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.clan_health.commands.player import prepare_player_health_window
from elbow_helper.features.help.discovery import ParameterInfo

from ..models import AgentAttachment
from .outcomes import CommandOutcome
from .registry import CommandAdapter


async def run_opinion(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    ticket = str(values.get("ticket") or "").strip()
    if not ticket:
        return CommandOutcome.needs_input("ticket")
    workflow = context.bot.get_cog("Recruitment")
    if workflow is None:
        return CommandOutcome.unavailable()
    channel = workflow.resolve_opinion_ticket(context.guild, context.member, ticket)
    if channel is None:
        return CommandOutcome.needs_input("ticket")
    parts = await workflow._build_ticket_second_opinion(channel)
    if not parts:
        return CommandOutcome("empty", "private")
    return CommandOutcome("complete", "private", private_parts=tuple(parts))


async def run_health_player(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    raw_account = values.get("account")
    player_tag = normalize_player_tag(raw_account) if isinstance(raw_account, str) else None
    if player_tag is None:
        return CommandOutcome.needs_input("account")
    mode = values.get("period", "last_30d")
    if mode == "custom" and (not values.get("date_from") or not values.get("date_to")):
        return CommandOutcome.needs_input("date_from and date_to")
    selected, issue = prepare_player_health_window(
        mode, date_from=values.get("date_from"), date_to=values.get("date_to"),
    )
    if issue or selected is None:
        return CommandOutcome.needs_input("period")
    workflow = context.bot.get_cog("ClanHealth")
    if workflow is None:
        return CommandOutcome.unavailable()
    report = await workflow.build_player_health_export(
        player_tag=player_tag, now=selected.now, window_mode=selected.mode,
        season_key=selected.season_key, trend_season_key=selected.trend_season_key,
        cycle_start=selected.start, cycle_end=selected.end,
        window_label=selected.label, date_from=selected.date_from,
        date_to=selected.date_to,
    )
    if report is None:
        return CommandOutcome("empty")
    path = await workflow.write_health_workbook(report.sheets)
    try:
        link, warning = await workflow.google_publisher.upload_workbook(path, report.workbook_title)
        if link:
            return CommandOutcome("complete", text=f"{report.workbook_title}\n{link}")
        data = await asyncio.to_thread(path.read_bytes)
        lines = [*report.summary_lines]
        if warning:
            lines.append(warning)
        return CommandOutcome(
            "complete", text="\n".join(lines),
            attachments=(AgentAttachment(report.workbook_name, data),),
        )
    finally:
        await asyncio.to_thread(workflow.local_exports.delete, path)


def enabled_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter("/opinion", "private", run_opinion),
        CommandAdapter("/health player", "public", run_health_player, (
            ParameterInfo("date_from", "Start date for Custom dates.", False, "string"),
            ParameterInfo("date_to", "End date for Custom dates.", False, "string"),
        )),
    )
