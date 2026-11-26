from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks
from elbow_helper.infrastructure.discord.interactions import deny

from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import APPLICANT_ROLE_ID, CORE, RECRUITERS
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX, DEFAULT_THUMBNAIL_URL

from .config import APPLICANT_LINGER_DAYS, MAX_OVERDUE_APPLICANTS_DISPLAY
from .state import platform_totals
from .views import ApplicantCleanupView
from .weeks import WEEK, week_label, week_start

LOGGER = logging.getLogger(__name__)


class ReportsMixin:
    async def close_applicant_report(self, report_message_id: int, guild: discord.Guild) -> None:
        """Close an overdue-applicant report and disable its button."""
        key = str(report_message_id)

        def close(connection, state):
            report = state["applicant_reports"].get(key)
            if not report or not report.get("active"):
                return None
            report["active"] = False
            return ()
        await self.lifecycle_state.change(close)
        try:
            channel = guild.get_channel(OVERSEEING_TERRACE)
            if channel and isinstance(channel, discord.TextChannel):
                message = await channel.fetch_message(report_message_id)
                await message.edit(view=ApplicantCleanupView(self, report_message_id, disabled=True))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.debug("Failed to disable applicant cleanup view for %s", report_message_id)

    @staticmethod
    def _build_applicant_linger_embed(
        lingering: list[tuple[discord.Member, datetime, int]],
        owner_links: dict[str, list[str]],
        now: datetime,
    ) -> discord.Embed:
        entries: list[str] = []
        for index, (member, _, days) in enumerate(
            lingering[:MAX_OVERDUE_APPLICANTS_DISPLAY],
            start=1,
        ):
            links = owner_links.get(str(member.id), [])
            latest_ticket_link = links[-1] if links else None
            ticket_value = (
                f"[Open ticket log]({latest_ticket_link})"
                if latest_ticket_link
                else "No ticket log found"
            )
            day_noun = "day" if days == 1 else "days"
            entries.append(
                f"**{index}.** {member.mention}\n"
                f"Joined **{days} {day_noun} ago** · {ticket_value}"
            )

        embed = discord.Embed(
            title="🔔 Overdue Applicants",
            description="\n\n".join(entries),
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=now,
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)

        if len(lingering) > MAX_OVERDUE_APPLICANTS_DISPLAY:
            embed.set_footer(
                text=f"Showing {MAX_OVERDUE_APPLICANTS_DISPLAY} of {len(lingering)} applicants."
            )

        return embed

    @tasks.loop(minutes=30)
    async def weekly_report(self):
        await self.bot.wait_until_ready()

        now = datetime.now(timezone.utc)
        this_week = week_start(now)
        if now < this_week + timedelta(hours=8):
            return
        finished = this_week - WEEK
        if self.state.get("last_reported_week") == finished.date().isoformat():
            return

        channel = self.bot.get_channel(OVERSEEING_TERRACE)
        if not isinstance(channel, discord.TextChannel):
            return

        embed = discord.Embed(
            title="📊 Weekly Recruitment Sources",
            description=week_label(finished),
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=now,
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)

        platforms = sorted(platform_totals(await self.lifecycle_state.stayed_joins(finished, finished + WEEK)).items(),
                           key=lambda row: -row[1])
        if platforms:
            for platform, count in platforms:
                embed.add_field(name=platform, value=str(count), inline=True)
        else:
            embed.add_field(name="No recruitment joins", value="No recruitment joins that week.", inline=False)

        await channel.send(embed=embed)
        await self.lifecycle_state.update(
            {"last_reported_week": finished.date().isoformat(), "last_weekly_report_iso": now.isoformat()})

    @weekly_report.before_loop
    async def before_weekly_report(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def applicant_linger_scan(self):
        await self.bot.wait_until_ready()

        now = datetime.now(timezone.utc)
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        overseer = guild.get_channel(OVERSEEING_TERRACE)
        if not isinstance(overseer, discord.TextChannel):
            return

        applicant_role = guild.get_role(APPLICANT_ROLE_ID)
        if not applicant_role:
            return

        members = list(guild.members)
        if not members:
            try:
                members = [member async for member in guild.fetch_members(limit=None)]
            except (discord.Forbidden, discord.HTTPException) as exc:
                LOGGER.warning("Failed to fetch members: %s", exc)
                return

        cutoff = now - timedelta(days=APPLICANT_LINGER_DAYS)
        lingering: list[tuple[discord.Member, datetime, int]] = []
        for member in members:
            if applicant_role not in member.roles:
                continue
            joined_at = (member.joined_at or now).replace(tzinfo=timezone.utc)
            if joined_at <= cutoff:
                lingering.append((member, joined_at, (now - joined_at).days))

        # A report closes once nobody on it is still an applicant.
        applicant_ids = {member.id for member in members if applicant_role in member.roles}
        for key, report in list(self.state.get("applicant_reports", {}).items()):
            listed = {int(value) for value in report.get("applicant_ids", []) if str(value).isdigit()}
            if report.get("active") and not listed & applicant_ids:
                await self.close_applicant_report(int(key), guild)

        current_overdue_ids = {member.id for member, _, _ in lingering}
        previous_overdue_ids: set[int] = set()
        for member_id in self.state.get("overdue_applicant_ids", []):
            try:
                previous_overdue_ids.add(int(member_id))
            except (TypeError, ValueError):
                continue
        has_new_overdue_applicants = bool(current_overdue_ids - previous_overdue_ids)

        if not has_new_overdue_applicants:
            await self.lifecycle_state.update({"last_applicant_scan_iso": now.isoformat(),
                                               "overdue_applicant_ids": sorted(current_overdue_ids)})
            return

        await self._refresh_ticket_log_index(guild)
        owner_links = self.state.get("ticket_owner_links", {})
        lingering.sort(key=lambda row: row[2], reverse=True)

        embed = self._build_applicant_linger_embed(lingering, owner_links, now)

        msg = await overseer.send(embed=embed)
        await msg.edit(view=ApplicantCleanupView(self, msg.id))
        report = {
            "applicant_ids": [member.id for member, _, _ in lingering],
            "created_iso": now.isoformat(),
            "active": True,
        }

        def edit(connection, state):
            state["applicant_reports"][str(msg.id)] = report
            state["last_applicant_scan_iso"] = now.isoformat()
            state["overdue_applicant_ids"] = sorted(current_overdue_ids)
            return ()
        await self.lifecycle_state.change(edit)

    @applicant_linger_scan.before_loop
    async def before_applicant_linger_scan(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="recstats",
        description="See how many applicants came from each recruitment platform this week.",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def recruitment_stats(self, interaction: discord.Interaction):
        if not any(role.id in (CORE | RECRUITERS) for role in getattr(interaction.user, "roles", [])):
            await deny(interaction)
            return

        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            now = datetime.now(timezone.utc)
            this_week = week_start(now)
            embed = discord.Embed(
                title="Weekly Recruitment Stats",
                description=f"📊 {week_label(this_week)}\n\u200b",
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=now,
            )
            embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)

            platforms = sorted(platform_totals(await self.lifecycle_state.stayed_joins(this_week, this_week + WEEK)).items(),
                               key=lambda row: -row[1])
            total = sum(count for _, count in platforms)
            if platforms:
                for platform, count in platforms:
                    noun = "join" if count == 1 else "joins"
                    embed.add_field(name=platform, value=f"**{count}** {noun}", inline=False)
            else:
                embed.add_field(name="No recruitment joins", value="No recruitment joins this week.", inline=False)

            embed.set_footer(text=f"Total joins: {total}")
            msg = await interaction.followup.send(embed=embed, ephemeral=False)

            async def _cleanup(message: discord.Message):
                try:
                    await asyncio.sleep(86400)
                    await message.delete()
                except (asyncio.CancelledError, discord.NotFound):
                    return
                except (discord.Forbidden, discord.HTTPException):
                    LOGGER.debug("Failed to auto-delete recstats message %s", message.id)

            self._start_report_cleanup(_cleanup(msg), message_id=msg.id)
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Failed building recruitment stats")
            await interaction.followup.send(
                "I couldn't build the recruitment stats right now. Try again in a moment.",
                ephemeral=True,
            )
