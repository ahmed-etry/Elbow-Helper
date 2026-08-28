"""Monthly war-summary workflow for clan-reporting threads."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord.ext import tasks

from elbow_helper.configuration.clans import CLAN_NAMES, CLAN_TAGS
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX

from .charts import build_war_summary_chart
from .config import CLANS
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
        if self.state.get("last_summary_month") == month_key:
            return

        await self._post_monthly_war_summaries(month_start, month_end)
        self.state["last_summary_month"] = month_key
        save_state(self.state)

    async def _post_monthly_war_summaries(
        self, month_start: datetime, month_end: datetime
    ) -> None:
        for clan_code in CLANS:
            await self._post_war_summary_for_clan(clan_code, month_start, month_end)
            await asyncio.sleep(0.5)

    async def _post_war_summary_for_clan(
        self, clan_code: str, month_start: datetime, month_end: datetime
    ) -> None:
        thread = await self._get_thread(clan_code)
        if not thread:
            return

        clan_tag = CLAN_TAGS.get(clan_code)
        if not clan_tag:
            return

        war_log = await self._fetch_war_log(clan_tag, limit=50)
        wins, losses, ties = self._count_war_results(war_log, month_start, month_end)
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
        if war_log is None:
            embed = _new_summary_embed()
            embed.description = "The war log isn't available."
        elif total == 0:
            embed = _new_summary_embed()
            embed.description = "No wars recorded for this month."
        else:
            chart_file = build_war_summary_chart(clan_label, month_label, wins, losses, ties)
            if not chart_file:
                LOGGER.warning("Skipping monthly war summary for %s because chart rendering is unavailable", clan_code)
                return

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
            return

        summaries[clan_code] = new_message.id
        save_state(self.state)

    def _count_war_results(
        self, war_log: Optional[list[dict[str, Any]]], month_start: datetime, month_end: datetime
    ) -> tuple[int, int, int]:
        if not war_log:
            return 0, 0, 0

        wins = losses = ties = 0
        for entry in war_log:
            end_time = self._parse_warlog_time(entry.get("endTime"))
            if not end_time or not (month_start <= end_time < month_end):
                continue

            result = (entry.get("result") or "").lower()
            if result == "win":
                wins += 1
            elif result == "lose":
                losses += 1
            elif result == "tie":
                ties += 1
        return wins, losses, ties
