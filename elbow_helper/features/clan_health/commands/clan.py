
"""Clan-health export command."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from elbow_helper.infrastructure.discord.interactions import deny
from elbow_helper.infrastructure.discord.interactions import warn
from elbow_helper.configuration.clans import CLAN_NAMES

from ..config import CLAN_EXPORT_ORDER, UTC
from ..export import HealthWorkbookError, PreparedHealthExport

LOGGER = logging.getLogger(__name__)


def roster_row(clan_code: str, member: Dict[str, Any]) -> Dict[str, Any]:
    """A roster member before analysis: activity starts empty and progress unknown until the overlays fill it."""
    tag = str(member["player_tag"])
    return {
        "clan_code": clan_code, "player_tag": tag, "player_name": str(member.get("player_name") or tag),
        "townhall": None, "hero_sum": None, "pet_sum": None, "equipment_sum": None, "troop_sum": None,
        "spell_sum": None, "games_total": None, "capital_contrib": None, "war_stars": None, "attack_wins": None,
        "war_hits_used": 0, "war_hits_expected": 0, "war_missed": 0, "war_stars_total": 0.0,
        "war_destruction_total": 0.0, "war_attack_count": 0, "raid_attacks": 0, "raid_expected": 0,
        "raid_loot": 0, "raid_expected_estimated": False, "donations": 0, "donations_received": 0,
        "hero_delta": None, "pet_delta": None, "equipment_delta": None, "troop_delta": None,
        "spell_delta": None, "capital_delta": None, "th_delta": None, "games_delta": None,
        "flags": [], "status": "Good", "note": "",
    }


@dataclass(frozen=True, slots=True)
class ClanHealthCommandResult:
    status: str
    issue: str = ""
    export: PreparedHealthExport | None = None
    timeframe_key: str = ""


class ClanHealthClanCommandMixin:
    async def _export_clan_health(
        self,
        interaction: discord.Interaction,
        clan: app_commands.Choice[str],
        window: Optional[app_commands.Choice[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> None:
        started = time.monotonic()
        if not self._has_access(interaction):
            LOGGER.info("Command denied /health clan user=%s", getattr(interaction.user, "id", None))
            await deny(interaction)
            return
        result = await self.run_clan_health_export(
            clan.value, mode=window.value if window else "last_30d",
            date_from=date_from, date_to=date_to,
            on_valid=lambda: interaction.response.defer(thinking=True),
        )
        if result.status == "invalid_window":
            await warn(interaction, result.issue)
            return
        if result.status == "invalid_clan":
            await interaction.response.send_message(
                result.issue,
                ephemeral=True,
            )
            return
        if result.status == "empty":
            await interaction.followup.send(result.issue)
            return
        if result.status == "unavailable":
            await interaction.followup.send(result.issue)
            return
        if result.export is None:
            LOGGER.error("Clan health export completed without a workbook: clan=%s", clan.value)
            await interaction.followup.send(
                "Could not generate the spreadsheet right now. Try again in a moment.",
            )
            return
        await self.send_prepared_health_export(interaction, result.export)
        LOGGER.debug(
            "Command done /health clan user=%s clan=%s window=%s elapsed=%.2fs",
            getattr(interaction.user, "id", None),
            clan.value,
            result.timeframe_key,
            time.monotonic() - started,
        )

    async def run_clan_health_export(
        self, clan_code: str, *, mode: str = "last_30d",
        date_from: str | None = None, date_to: str | None = None,
        on_valid: Callable[[], Awaitable[Any]] | None = None,
    ) -> ClanHealthCommandResult:
        now = datetime.now(UTC)
        window_mode = mode
        if window_mode == "last_7d":
            timeframe_key = "last_7d"
            timeframe_label = "Last 7 days"
            cycle_end = now
            cycle_start = now - timedelta(days=7)
        elif window_mode == "last_14d":
            timeframe_key = "last_14d"
            timeframe_label = "Last 14 days"
            cycle_end = now
            cycle_start = now - timedelta(days=14)
        elif window_mode == "custom":
            if not date_from or not date_to:
                return ClanHealthCommandResult(
                    "invalid_window", "Enter both a start date and an end date in YYYY-MM-DD format.",
                )
            try:
                cycle_start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                return ClanHealthCommandResult(
                    "invalid_window", f"`{date_from}` isn't a valid start date. Use YYYY-MM-DD.",
                )
            try:
                cycle_end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                return ClanHealthCommandResult(
                    "invalid_window", f"`{date_to}` isn't a valid end date. Use YYYY-MM-DD.",
                )
            if cycle_start >= cycle_end:
                return ClanHealthCommandResult(
                    "invalid_window", "The start date must be before the end date.",
                )
            if (cycle_end - cycle_start).days > 365:
                return ClanHealthCommandResult(
                    "invalid_window", "Choose a date range of 365 days or less.",
                )
            timeframe_key = f"custom_{date_from}_{date_to}"
            timeframe_label = f"Custom: {date_from} to {date_to}"
        else:
            timeframe_key = "last_30d"
            timeframe_label = "Last 30 days"
            cycle_end = now
            cycle_start = now - timedelta(days=30)

        if clan_code != "ALL" and clan_code not in CLAN_EXPORT_ORDER:
            return ClanHealthCommandResult(
                "invalid_clan", "Clan Health reports aren't available for that clan.",
            )

        selected_clans = CLAN_EXPORT_ORDER if clan_code == "ALL" else [clan_code]
        if on_valid is not None:
            await on_valid()

        warnings: List[str] = []
        # Each clan's members at the end of the period, from the family record; verdicts are worked out below.
        roster_ts, rosters = await asyncio.to_thread(
            self.record.roster, selected_clans, at_ts=int(cycle_end.timestamp()),
        )
        if not all(rosters.get(code) for code in selected_clans):
            return ClanHealthCommandResult(
                "empty",
                "No clan roster is available near the end of that period. Try a wider date range, or wait until more history is available.",
            )
        clan_entries: List[Dict[str, Any]] = [
            {"clan_code": code, "clan_name": CLAN_NAMES[code],
             "players": [roster_row(code, member) for member in rosters[code]]}
            for code in selected_clans
        ]
        if roster_ts:
            snapshot_dt = datetime.fromtimestamp(int(roster_ts), tz=UTC)
            warnings.append(f"Roster from {snapshot_dt.strftime('%Y-%m-%d %H:%M UTC')}.")
        else:
            warnings.append("Using the latest available roster.")

        sheets, all_rows, overall_totals = await asyncio.to_thread(
            self.analyzer.build_sheets,
            selected_clans=selected_clans,
            clan_entries=clan_entries,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        workbook_name = f"clan_health_{clan_code.lower()}_{timeframe_key}_{timestamp}.xlsx"
        workbook_title = f"**Clan Health ({clan_code}) - {timeframe_label}**"
        needs_count = sum(1 for row in all_rows if str(row.get("status") or "") == "Needs Review")
        watch_count = sum(1 for row in all_rows if str(row.get("status") or "") == "Watch")
        healthy_count = sum(1 for row in all_rows if str(row.get("status") or "") == "Good")
        summary_lines = [
            f"Clan Health report for `{clan_code}`.",
            f"Period: {timeframe_label}",
            f"Dates: {cycle_start.date().isoformat()} to {cycle_end.date().isoformat()}",
            f"Members: {len(all_rows)} ({healthy_count} Good, {watch_count} Watch, {needs_count} Needs Review)",
        ]
        per_clan = (overall_totals or {}).get("per_clan") or {}
        if per_clan:
            verdict_parts = [f"{code}: {str(verdict or 'Insufficient data')}" for code, verdict in per_clan.items()]
            summary_lines.append("Overall: " + " | ".join(verdict_parts))
        if warnings:
            preview = " | ".join(warnings[:4])
            suffix = f" (+{len(warnings) - 4} more)" if len(warnings) > 4 else ""
            summary_lines.append(f"Notes: {preview}{suffix}")

        try:
            export = await self.prepare_health_export(
                workbook_name=workbook_name,
                workbook_title=workbook_title,
                summary_lines=summary_lines,
                sheets=sheets,
            )
        except HealthWorkbookError:
            return ClanHealthCommandResult(
                "unavailable", "Could not generate the spreadsheet right now. Try again in a moment.",
            )
        return ClanHealthCommandResult(
            "complete", export=export, timeframe_key=timeframe_key,
        )
