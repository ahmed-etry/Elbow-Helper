"""Lifecycle wiring"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from discord.ext import commands

from .board import WarBoardMixin
from .config import CLAN_CHANNELS
from .emojis import WarEmojiProvider
from .helpers import HelperMixin
from .record import RecordMixin
from .roles import WarRoleMixin
from .roles import RosterRoleClaim
from .state import StateMixin, load_cache, save_cache
from .tasks import TaskMixin
from .warflow import WarflowMixin
from .queries import WarQueries


class WarManager(
    StateMixin,
    RecordMixin,
    HelperMixin,
    WarBoardMixin,
    WarRoleMixin,
    WarflowMixin,
    TaskMixin,
    commands.Cog,
):
    """
    Drives boards, roles and leadership summaries from the family record's war checks.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.war_reads = getattr(bot, "war_reads", None)
        self.account_links = None
        self._roster_role_claim: RosterRoleClaim | None = None
        # Copy static routing config so runtime state never mutates shared constants.
        self.clan_channels: Dict[str, Dict[str, int]] = {
            clan: dict(meta) for clan, meta in CLAN_CHANNELS.items()
        }

        self.cache = load_cache()
        processed_wars = self.cache.get("processed_wars", [])
        self.processed_war_order = (
            [war_id for war_id in processed_wars if isinstance(war_id, str) and war_id]
            if isinstance(processed_wars, list)
            else []
        )
        self.processed_war_ids = set(self.processed_war_order)
        if self._prune_processed_wars():
            save_cache(self.cache)
        # Runtime per-clan state used for transition detection and dedupe.
        self.war_context: Dict[str, Dict[str, Any]] = {}
        # Registry of summary message IDs used by periodic TTL cleanup.
        self.summary_registry: Dict[str, Dict[str, int]] = self._load_summary_registry()
        self.war_board_registry: Dict[str, Dict[str, int]] = (
            self._load_war_board_registry()
        )
        # The record's latest current and previous war per board, and when each was checked.
        self.record_boards: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.war_observations: Dict[str, Dict[str, Any]] = {}
        self.queries = WarQueries(self)
        (
            self.war_role_lineups,
            self.war_role_managed_members,
        ) = self._load_war_role_state()
        self.war_emojis = WarEmojiProvider(bot)

        self._summary_cleanup_task = None
        self._startup_sync_task = None
        self._startup_sync_done = asyncio.Event()
        self._war_state_locks: Dict[str, asyncio.Lock] = {}
        self._war_summary_state_lock = asyncio.Lock()
        self._war_role_locks: Dict[str, asyncio.Lock] = {}
        self._war_role_missing_links: Dict[str, set[str]] = {}
        self._wars_in_flight: Dict[str, set[str]] = {}
        self._record_checked: Dict[str, int] = {}
        self._record_views: set[str] = set()
        self._record_lock = asyncio.Lock()

    def set_account_links(self, account_links) -> None:
        """Supply the account-link reader after its extension loads."""
        self.account_links = account_links

    @commands.Cog.listener()
    async def on_ready(self):
        # The collectors' war passes drive every refresh after the startup replay.
        if self._startup_sync_task is None:
            self._startup_sync_done.clear()
            self._startup_sync_task = asyncio.create_task(self._sync_war_state_from_record())
        if self._summary_cleanup_task is None or self._summary_cleanup_task.done():
            self._summary_cleanup_task = asyncio.create_task(self._periodic_summary_cleanup())

    @commands.Cog.listener()
    async def on_family_wars_collected(self):
        # The startup replay covers passes that finish before it does.
        if self._startup_sync_done.is_set():
            await self._refresh_from_record()

    def cog_unload(self):
        if self._summary_cleanup_task:
            self._summary_cleanup_task.cancel()
        if self._startup_sync_task:
            self._startup_sync_task.cancel()
