
"""Clan-health export command."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import warn
from elbow_helper.configuration.clans import CLAN_NAMES

from ..config import CLAN_EXPORT_ORDER, UTC

LOGGER = logging.getLogger(__name__)


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
        if not self.clash_client.configured:
            LOGGER.error("Command blocked /health clan reason=missing_coc_api_key")
            await interaction.response.send_message(
                "Clash data isn't available because the connection hasn't been set up.",
                ephemeral=True,
            )
            return

        now = datetime.now(UTC)
        window_mode = window.value if window else "last_30d"
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
                await warn(interaction, "Enter both a start date and an end date in YYYY-MM-DD format.")
                return
            try:
                cycle_start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                await warn(interaction, f"`{date_from}` isn't a valid start date. Use YYYY-MM-DD.")
                return
            try:
                cycle_end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                await warn(interaction, f"`{date_to}` isn't a valid end date. Use YYYY-MM-DD.")
                return
            if cycle_start >= cycle_end:
                await warn(interaction, "The start date must be before the end date.")
                return
            if (cycle_end - cycle_start).days > 365:
                await warn(interaction, "Choose a date range of 365 days or less.")
                return
            timeframe_key = f"custom_{date_from}_{date_to}"
            timeframe_label = f"Custom: {date_from} to {date_to}"
        else:
            timeframe_key = "last_30d"
            timeframe_label = "Last 30 days"
            cycle_end = now
            cycle_start = now - timedelta(days=30)

        if clan.value != "ALL" and clan.value not in CLAN_EXPORT_ORDER:
            await interaction.response.send_message(
                "Clan Health reports aren't available for that clan.",
                ephemeral=True,
            )
            return

        selected_clans = CLAN_EXPORT_ORDER if clan.value == "ALL" else [clan.value]
        await interaction.response.defer(thinking=True)

        warnings: List[str] = []
        clan_entries: List[Dict[str, Any]] = []
        run_meta, stored = await asyncio.to_thread(
            self.repository.latest_report_before,
            cycle_end_ts=int(cycle_end.timestamp()),
            selected_clans=selected_clans,
        )
        if not stored:
            await interaction.followup.send(
                "No clan roster is available near the end of that period. Try a wider date range, or wait until more history is available."
            )
            return
        # Clan health is DB-first: decode stored rows and build export from cache.
        grouped = {code: {"clan_code": code, "clan_name": CLAN_NAMES[code], "players": []} for code in selected_clans}
        for row in stored:
            flags = []
            try:
                flags = json.loads(row.get("flags_json") or "[]")
            except (json.JSONDecodeError, TypeError, ValueError):
                flags = []
            row["flags"] = flags
            grouped[row["clan_code"]]["players"].append(row)
        clan_entries = [grouped[code] for code in selected_clans]
        if run_meta and run_meta.get("created_ts"):
            snapshot_dt = datetime.fromtimestamp(int(run_meta["created_ts"]), tz=UTC)
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
        workbook_name = f"clan_health_{clan.value.lower()}_{timeframe_key}_{timestamp}.xlsx"
        workbook_title = f"**Clan Health ({clan.value}) - {timeframe_label}**"
        needs_count = sum(1 for row in all_rows if str(row.get("status") or "") == "Needs Review")
        watch_count = sum(1 for row in all_rows if str(row.get("status") or "") == "Watch")
        healthy_count = sum(1 for row in all_rows if str(row.get("status") or "") == "Good")
        summary_lines = [
            f"Clan Health report for `{clan.value}`.",
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

        await self._write_and_send_export(
            interaction=interaction,
            workbook_name=workbook_name,
            workbook_title=workbook_title,
            summary_lines=summary_lines,
            sheets=sheets,
        )
        LOGGER.debug(
            "Command done /health clan user=%s clan=%s window=%s elapsed=%.2fs",
            getattr(interaction.user, "id", None),
            clan.value,
            timeframe_key,
            time.monotonic() - started,
        )
