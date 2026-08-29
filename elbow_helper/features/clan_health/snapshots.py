
"""Background ingestion and snapshot-cycle logic for clan health."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from discord.ext import tasks

from .config import CLAN_ORDER, SNAPSHOT_LOG_MINUTES, UTC
from .seasons import _clan_games_signal_open

LOGGER = logging.getLogger(__name__)


class ClanHealthSnapshotMixin:
    @tasks.loop(minutes=20)
    async def _war_log_loop(self) -> None:
        if not self.clash_client.configured:
            return
        async with self._background_lock:
            ingested_wars, ingested_rows, ingest_warnings = await self.collector.ingest_family_war_activity()
            if ingest_warnings:
                LOGGER.info(
                    "War ingest warnings=%s top=%s wars=%s rows=%s",
                    len(ingest_warnings),
                    self._warning_preview(ingest_warnings, limit=3),
                    ingested_wars,
                    ingested_rows,
                )
            self._last_war_ingest_ts = int(time.time())

    @_war_log_loop.before_loop
    async def _war_log_loop_before(self) -> None:
        await self._wait_for_boot_complete()
        self._log_startup_once()

    async def _run_background_snapshot_cycle(self, *, trigger: str) -> None:
        if not self.clash_client.configured:
            return
        now = datetime.now(UTC)
        # Rolling 8-week window covers every weekend/war the CoC API returns.
        cycle_end = now
        cycle_start = now - timedelta(weeks=8)
        partial = False
        season_key = "rolling"

        warnings: List[str] = []
        clan_entries: List[Dict[str, Any]] = []
        ingested_wars = 0
        ingested_rows = 0
        now_ts = int(now.timestamp())
        # Keep family war activity fresh for participation/performance overlays.
        if (now_ts - int(self._last_war_ingest_ts)) > 900:
            ingested_wars, ingested_rows, ingest_warnings = await self.collector.ingest_family_war_activity()
            self._last_war_ingest_ts = now_ts
            warnings.extend(ingest_warnings)
        # Build a consistent all-family snapshot for this rolling window.
        for code in CLAN_ORDER:
            entry, entry_warnings = await self.collector.collect_clan_live(
                clan_code=code,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            clan_entries.append(entry)
            warnings.extend(entry_warnings)
            if entry.get("roster_complete") is not True:
                partial = True

        all_rows: List[Dict[str, Any]] = []
        for entry in clan_entries:
            all_rows.extend(entry.get("players", []))

        await asyncio.to_thread(
            self.analyzer.apply_war_activity,
            all_rows,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
        )
        captured_ts = int(now.timestamp())
        await asyncio.to_thread(self.repository.store_snapshots, captured_ts, all_rows)
        await asyncio.to_thread(
            self.analyzer.apply_raid_activity,
            all_rows,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
        )
        await asyncio.to_thread(
            self.analyzer.apply_donation_activity,
            all_rows,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
        )
        await asyncio.to_thread(
            self.analyzer.apply_progression_fallback,
            all_rows,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
        )
        cg_signal_enabled = _clan_games_signal_open(
            cycle_start=cycle_start,
            cycle_end=cycle_end,
        )
        for row in all_rows:
            row["cg_signal_disabled"] = not bool(cg_signal_enabled)
        await asyncio.to_thread(
            self.analyzer.apply_flags,
            all_rows,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
        )
        run_id = f"background:{season_key}:all:{captured_ts}"
        # Persist one report row set that commands can read without API calls.
        await asyncio.to_thread(
            self.repository.store_report,
            run_id=run_id,
            created_ts=captured_ts,
            season_key=season_key,
            scope="BACKGROUND_ALL",
            partial=partial,
            cycle_start_ts=int(cycle_start.timestamp()),
            cycle_end_ts=int(cycle_end.timestamp()),
            rows=all_rows,
        )
        self._last_snapshot_ts = captured_ts
        warning_top = self._warning_preview(warnings, limit=1)
        log_sig: Tuple[Any, ...] = (
            season_key,
            len(all_rows),
            len(CLAN_ORDER),
            partial,
            ingested_wars,
            ingested_rows,
            len(warnings),
            warning_top,
        )
        if trigger == "startup":
            self._last_startup_sync_ts = captured_ts
        suppress_immediate_loop_log = (
            trigger == "loop"
            and self._last_startup_sync_ts
            and (captured_ts - int(self._last_startup_sync_ts)) < max(300, SNAPSHOT_LOG_MINUTES * 60)
        )
        should_log_info = (
            not suppress_immediate_loop_log
            and (
                trigger == "startup"
                or self._last_background_log_sig != log_sig
                or (captured_ts - int(self._last_background_log_ts)) >= 21600
            )
        )
        if should_log_info:
            warning_count = len(warnings)
            if warning_count:
                LOGGER.info(
                    "Background sync season=%s players=%s warnings=%s top=%s",
                    season_key,
                    len(all_rows),
                    warning_count,
                    warning_top,
                )
            else:
                LOGGER.info(
                    "Background sync season=%s players=%s warnings=0",
                    season_key,
                    len(all_rows),
                )
            LOGGER.debug(
                "Background sync details trigger=%s clans=%s partial=%s war_sync=%s/%s",
                trigger,
                len(CLAN_ORDER),
                partial,
                ingested_wars,
                ingested_rows,
            )
            self._last_background_log_sig = log_sig
            self._last_background_log_ts = captured_ts

    @tasks.loop(minutes=SNAPSHOT_LOG_MINUTES)
    async def _snapshot_log_loop(self) -> None:
        if not self.clash_client.configured:
            return
        async with self._background_lock:
            await self._run_background_snapshot_cycle(trigger="loop")

    @_snapshot_log_loop.before_loop
    async def _snapshot_log_loop_before(self) -> None:
        await self._wait_for_boot_complete()
        self._log_startup_once()
        if not self.clash_client.configured:
            return
        async with self._background_lock:
            await self._run_background_snapshot_cycle(trigger="startup")
