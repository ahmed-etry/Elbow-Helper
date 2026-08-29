
"""Clan-health cog shell and lifecycle wiring."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any, List, Optional, Tuple

import discord
from discord.ext import commands

from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import LocalExportStore
from elbow_helper.configuration.roles import LEAD_PLUS

from .analysis import ClanHealthAnalyzer
from .api import ClanHealthCollector
from .commands import ClanHealthCommandMixin
from .database import ClanHealthRepository
from .export import ClanHealthExportMixin
from .snapshots import ClanHealthSnapshotMixin


class ClanHealth(
    commands.Cog,
    ClanHealthSnapshotMixin,
    ClanHealthExportMixin,
    ClanHealthCommandMixin,
):
    def __init__(
        self,
        bot: commands.Bot,
        clash_client: ClashClient,
        google_publisher: GoogleSheetsPublisher,
        local_exports: LocalExportStore,
        repository: ClanHealthRepository,
        analyzer: ClanHealthAnalyzer,
        collector: ClanHealthCollector,
    ):
        self.bot = bot
        self.clash_client = clash_client
        self.google_publisher = google_publisher
        self.local_exports = local_exports
        self.repository = repository
        self.analyzer = analyzer
        self.collector = collector
        self._last_war_ingest_ts = 0
        self._last_snapshot_ts = 0
        self._last_background_log_sig: Optional[Tuple[Any, ...]] = None
        self._last_background_log_ts = 0
        self._last_startup_sync_ts = 0
        self._background_lock = asyncio.Lock()
        self._startup_logged = False
        self.repository.initialize()
        self._war_log_loop.start()
        self._snapshot_log_loop.start()

    async def cog_load(self) -> None:
        from .database.config_store import seed_missing_configs

        await asyncio.to_thread(seed_missing_configs)

    def cog_unload(self) -> None:
        if self._war_log_loop.is_running():
            self._war_log_loop.cancel()
        if self._snapshot_log_loop.is_running():
            self._snapshot_log_loop.cancel()
        self.collector.close()

    async def _wait_for_boot_complete(self) -> None:
        await self.bot.wait_until_ready()
        boot_event = getattr(self.bot, "boot_complete", None)
        if isinstance(boot_event, asyncio.Event):
            await boot_event.wait()

    def _log_startup_once(self) -> None:
        if self._startup_logged:
            return
        self._startup_logged = True

    @staticmethod
    def _has_access(interaction: discord.Interaction) -> bool:
        user = interaction.user
        return any(getattr(role, "id", None) in LEAD_PLUS for role in getattr(user, "roles", []))

    @staticmethod
    def _warning_preview(warnings: List[str], limit: int = 3) -> str:
        if not warnings:
            return "none"
        counter = Counter(str(w).strip() for w in warnings if str(w).strip())
        if not counter:
            return "none"
        top = ", ".join(f"{text} ({count})" for text, count in counter.most_common(max(1, limit)))
        extra = len(counter) - min(len(counter), max(1, limit))
        return f"{top}{f' +{extra} more' if extra > 0 else ''}"
