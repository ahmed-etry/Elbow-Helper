
"""Clan-health cog shell and lifecycle wiring."""

from __future__ import annotations

import asyncio

import discord
from discord.ext import commands

from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import LocalExportStore
from elbow_helper.configuration.roles import LEAD_PLUS

from .analysis import ClanHealthAnalyzer
from .commands import ClanHealthCommandMixin
from .config import DB_PATH, OLD_DB_PATH
from .export import ClanHealthExportMixin
from .record import HealthRecord


class ClanHealth(
    commands.Cog,
    ClanHealthExportMixin,
    ClanHealthCommandMixin,
):
    """Health reports, worked out from the family record when asked for; nothing is collected here."""

    def __init__(
        self,
        bot: commands.Bot,
        google_publisher: GoogleSheetsPublisher,
        local_exports: LocalExportStore,
        record: HealthRecord,
        analyzer: ClanHealthAnalyzer,
    ):
        self.bot = bot
        self.google_publisher = google_publisher
        self.local_exports = local_exports
        self.record = record
        # The account directory health autocomplete, attack plans and CWL search.
        self.directory = record
        self.analyzer = analyzer
        self.regular_wars = analyzer.regular_wars
        self.league_wars = analyzer.league_wars

    async def cog_load(self) -> None:
        from .database.config_store import seed_missing_configs
        from .database.schema import copy_old_settings

        await asyncio.to_thread(copy_old_settings, DB_PATH, OLD_DB_PATH)
        await asyncio.to_thread(seed_missing_configs)

    @staticmethod
    def _has_access(interaction: discord.Interaction) -> bool:
        user = interaction.user
        return any(getattr(role, "id", None) in LEAD_PLUS for role in getattr(user, "roles", []))
