"""Monthly war-summary workflow for clan-reporting threads."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import tasks

from elbow_helper.configuration.clans import CLAN_NAMES, CLAN_TAGS
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX

from .charts import build_war_summary_chart
from .config import CLANS
from .record_wars import monthly_results
from .state import save_state

LOGGER = logging.getLogger(__name__)


class ClanReportingWarMixin:
    """War summary scheduling, counting, and posting."""

    @tasks.loop(minutes=30)
    async def _monthly_summary_loop(self):
        await self.bot.wait_until_ready()
        now = datetime.now(timezone.utc)
        month_key, month_start, month_end = self._previous_month_bounds(now)
        target_time = datetime(now.year, now.month, 1, 8, 0, tzinfo=timezone.utc)
        if now < target_time:
            return
        completed = await self._post_monthly_war_summaries(month_start, month_end)
        if completed and self.state.get("last_summary_month") != month_key:
            self.state["last_summary_month"] = month_key
            save_state(self.state)

    async def _post_monthly_war_summaries(
        self, month_start: datetime, month_end: datetime
    ) -> bool:
        month_key = month_start.strftime("%Y-%m")
        completed_months = self.state.setdefault("last_summary_month_by_clan", {})
        for clan_code in CLANS:
            if completed_months.get(clan_code) == month_key:
                continue
            try:
                completed = await self._post_war_summary_for_clan(clan_code, month_start, month_end)
            except discord.HTTPException as exc:
                LOGGER.warning("Failed to post monthly war summary for %s: %s", clan_code, exc)
            else:
                if completed:
                    completed_months[clan_code] = month_key
                    save_state(self.state)
            await asyncio.sleep(0.5)
        return all(completed_months.get(clan_code) == month_key for clan_code in CLANS)

    async def _post_war_summary_for_clan(
        self, clan_code: str, month_start: datetime, month_end: datetime
    ) -> bool:
        thread = await self._get_thread(clan_code)
        if not thread:
            return False

        clan_tag = CLAN_TAGS.get(clan_code)
        if not clan_tag:
            return False

        status, (wins, losses, ties) = await self._month_war_counts(clan_tag, month_start, month_end)
        if status == "waiting":
            return False
        log_available = status == "available"
        total = wins + losses + ties
        month_label = month_start.strftime("%B %Y")

        chart_file = None
        embed: Optional[discord.Embed] = None

        def _new_summary_embed() -> discord.Embed:
            return discord.Embed(
                title=f"War Summary - {month_label}",
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=datetime.now(timezone.utc),
            )

        clan_label = CLAN_NAMES.get(clan_code, clan_code)
        if not log_available:
            embed = _new_summary_embed()
            embed.description = "The war log isn't available."
        elif total == 0:
            embed = _new_summary_embed()
            embed.description = "No wars recorded for this month."
        else:
            chart_file = build_war_summary_chart(clan_label, month_label, wins, losses, ties)
            if not chart_file:
                LOGGER.warning("Skipping monthly war summary for %s because chart rendering is unavailable", clan_code)
                return False

        summaries = self.state.setdefault("war_summaries", {})
        old_message_id = summaries.get(clan_code)
        if old_message_id:
            old_message = await self._fetch_message(thread, old_message_id)
            if old_message:
                try:
                    await old_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    LOGGER.debug("Failed to delete previous summary for %s: %s", clan_code, exc)

        if chart_file:
            new_message = await thread.send(file=chart_file)
        elif embed is not None:
            new_message = await thread.send(embed=embed)
        else:
            LOGGER.warning("Nothing to post for %s monthly summary", clan_code)
            return False

        summaries[clan_code] = new_message.id
        save_state(self.state)
        return log_available

    async def _month_war_counts(
        self, clan_tag: str, month_start: datetime, month_end: datetime
    ) -> tuple[str, tuple[int, int, int]]:
        """Wins, losses and ties of the recorded war log's newest 50 entries ended in the month.

        The status is "available", "unavailable" (no log to count; the notice
        stays pending) or "waiting" (nothing to post yet, try again later).
        """
        if self.war_reads is None:
            LOGGER.warning("Monthly war summary for %s waits: the family record's war reads are unavailable", clan_tag)
            return "waiting", (0, 0, 0)
        try:
            recorded = await monthly_results(self.war_reads, clan_tag, month_start.strftime("%Y-%m"))
        except (sqlite3.Error, OSError, RuntimeError):
            LOGGER.exception("Failed to read the recorded war log for %s", clan_tag)
            return "waiting", (0, 0, 0)
        if recorded["status"] != "available":
            return "unavailable", (0, 0, 0)
        if not recorded["coverage"]["read_after_month"]:
            # A log read before the month ended may lack its last wars.
            LOGGER.info("Monthly war summary for %s waits for a war log read after the month", clan_tag)
            return "waiting", (0, 0, 0)
        return "available", (recorded["wins"], recorded["losses"], recorded["ties"])
