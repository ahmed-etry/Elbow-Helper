"""Command surface for the CWL roster planner."""

from __future__ import annotations

import asyncio
import logging
import sqlite3

import discord
from discord import app_commands

from elbow_helper.discord.interactions import deny
from elbow_helper.configuration.roles import LEAD_PLUS

from ..config import CWL_CLAN_CODES

LOGGER = logging.getLogger(__name__)

ROSTER_HISTORY_CHOICES = [
    app_commands.Choice(name="Latest 3 seasons", value=3),
    app_commands.Choice(name="Latest 6 seasons", value=6),
    app_commands.Choice(name="Latest 12 seasons", value=12),
    app_commands.Choice(name="All available seasons", value=0),
]


class CwlRosterMixin:
    async def prepare_cwl_roster(self, guild: discord.Guild,
                                 requested_limit: int) -> dict[str, object]:
        history_limit = requested_limit if requested_limit > 0 else None
        try:
            analysis = await asyncio.to_thread(
                self._analyze_roster_history, history_limit,
            )
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            LOGGER.exception("CWL roster analysis failed")
            return {"status": "unavailable",
                    "issue": "Couldn't load CWL history. Try again in a moment."}
        if not analysis["wars"]:
            return {"status": "empty",
                    "issue": "No completed CWL seasons are available yet."}
        try:
            candidate_data = await self._build_roster_candidates(
                guild=guild,
                season_metrics=analysis["season_metrics"],
                mega_metrics=analysis["mega_metrics"],
            )
        except RuntimeError as exc:
            return {"status": "unavailable", "issue": str(exc)}
        if not candidate_data["signed_account_count"]:
            return {"status": "empty",
                    "issue": "No accounts are signed up on the CWL Signup roster."}
        sheets = self._build_roster_workbook(
            candidates=candidate_data["candidates"],
            signed_tags=candidate_data["signed_tags"],
            season_metrics=analysis["season_metrics"],
            records=candidate_data["records"],
            links_by_user=candidate_data["links_by_user"],
            seasons=analysis["seasons"],
            profiles=analysis["profiles"],
            latest_leagues=analysis["latest_leagues"],
            clan_choices=CWL_CLAN_CODES,
        )
        history_label = (
            "all available seasons" if history_limit is None
            else f"latest {history_limit} available seasons per clan"
        )
        return {
            "status": "complete", "sheets": sheets,
            "history_label": history_label,
            "signed_member_count": int(candidate_data["signed_member_count"]),
            "signed_account_count": int(candidate_data["signed_account_count"]),
        }

    @app_commands.choices(history=ROSTER_HISTORY_CHOICES)
    @app_commands.describe(
        history="How many completed seasons to include (the latest three by default).",
    )
    async def cwl_roster(
        self,
        interaction: discord.Interaction,
        history: app_commands.Choice[int] | None = None,
    ) -> None:
        if not self._has_any_role(interaction, LEAD_PLUS):
            await deny(interaction)
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "Run this command in the server.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        requested_limit = int(history.value) if history is not None else 3
        prepared = await self.prepare_cwl_roster(interaction.guild, requested_limit)
        if prepared["status"] != "complete":
            await interaction.followup.send(prepared["issue"], ephemeral=True)
            return
        await self._send_roster_workbook(
            interaction=interaction,
            sheets=prepared["sheets"],
            history_label=prepared["history_label"],
            signed_member_count=prepared["signed_member_count"],
            signed_account_count=prepared["signed_account_count"],
        )
