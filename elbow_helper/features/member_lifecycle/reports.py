from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks
from elbow_helper.discord.interactions import deny

from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import APPLICANT_ROLE_ID, CORE, RECRUITERS
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX, DEFAULT_THUMBNAIL_URL

from .config import APPLICANT_LINGER_DAYS, MAX_OVERDUE_APPLICANTS_DISPLAY, WEEKLY_REPORT_INTERVAL_DAYS
from .state import save_state
from .views import ApplicantCleanupView

LOGGER = logging.getLogger(__name__)


class ReportsMixin:
    def _load_last_run(self, state_key: str, now: datetime, *, fallback_days: int) -> datetime:
        last_iso = self.state.get(state_key)
        if not last_iso:
            return now - timedelta(days=fallback_days)
        try:
            return datetime.fromisoformat(last_iso)
        except ValueError:
            return now - timedelta(days=fallback_days)

    @tasks.loop(hours=24)
    async def weekly_report(self):
        await self.bot.wait_until_ready()

        now = datetime.now(timezone.utc)
        last = self._load_last_run("last_weekly_report_iso", now, fallback_days=1000)

        if (now - last).days < WEEKLY_REPORT_INTERVAL_DAYS:
            return

        channel = self.bot.get_channel(OVERSEEING_TERRACE)
        if not isinstance(channel, discord.TextChannel):
            return

        iso_year, _, _ = now.isocalendar()
        month_label = now.strftime("%B")
        month_week = ((now.day - 1) // 7) + 1
        embed = discord.Embed(
            title="📊 Weekly Recruitment Sources",
            description=f"Week {month_week} • {month_label} {iso_year}",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=now,
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)

        platforms = sorted(self.state["platform_counts"].items(), key=lambda row: -row[1])
        if platforms:
            for platform, count in platforms:
                embed.add_field(name=platform, value=str(count), inline=True)
        else:
            embed.add_field(name="No recruitment joins", value="No recruitment joins this week.", inline=False)

        await channel.send(embed=embed)
        self.state["platform_counts"] = {}
        self.state["last_weekly_report_iso"] = now.isoformat()
        save_state(self.state)

    @weekly_report.before_loop
    async def before_weekly_report(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=168)
    async def applicant_linger_scan(self):
        await self.bot.wait_until_ready()

        now = datetime.now(timezone.utc)
        last = self._load_last_run("last_applicant_scan_iso", now, fallback_days=1000)
        if (now - last) < timedelta(days=WEEKLY_REPORT_INTERVAL_DAYS):
            return

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

        self.state["last_applicant_scan_iso"] = now.isoformat()

        if not lingering:
            save_state(self.state)
            return

        await self._refresh_ticket_log_index(guild)
        owner_links = self.state.get("ticket_owner_links", {})
        lingering.sort(key=lambda row: row[2], reverse=True)

        rows: list[str] = []
        for member, _, days in lingering:
            links = owner_links.get(str(member.id), [])
            latest_ticket_link = links[-1] if links else None
            ticket_value = f"[Yes]({latest_ticket_link})" if latest_ticket_link else "No"
            rows.append(f"• {member.mention} — {days}d — Ticket: {ticket_value}")

        display_rows = rows[:MAX_OVERDUE_APPLICANTS_DISPLAY]
        if len(rows) > MAX_OVERDUE_APPLICANTS_DISPLAY:
            extra = len(rows) - MAX_OVERDUE_APPLICANTS_DISPLAY
            noun = "applicant" if extra == 1 else "applicants"
            display_rows.append(f"+{extra} more {noun}")

        embed = discord.Embed(
            title="🔔 Overdue Applicants",
            description=f"Applicants who joined at least {APPLICANT_LINGER_DAYS} days ago.",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=now,
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(name="Applicants", value="\n".join(display_rows), inline=False)

        msg = await overseer.send(embed=embed)
        await msg.edit(view=ApplicantCleanupView(self, msg.id))
        self.state.setdefault("applicant_reports", {})[str(msg.id)] = {
            "applicant_ids": [member.id for member, _, _ in lingering],
            "created_iso": now.isoformat(),
            "active": True,
        }
        save_state(self.state)

    @applicant_linger_scan.before_loop
    async def before_applicant_linger_scan(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="recstats",
        description="See how many applicants came from each recruitment source this week.",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def recruitment_stats(self, interaction: discord.Interaction):
        if not any(role.id in (CORE | RECRUITERS) for role in getattr(interaction.user, "roles", [])):
            await deny(interaction)
            return

        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            now = datetime.now(timezone.utc)
            iso_year, _, _ = now.isocalendar()
            month_label = now.strftime("%B")
            month_week = ((now.day - 1) // 7) + 1
            embed = discord.Embed(
                title="Weekly Recruitment Stats",
                description=f"📊 Week {month_week} · {month_label} {iso_year}\n\u200b",
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=now,
            )
            embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)

            platforms = sorted(self.state["platform_counts"].items(), key=lambda row: -row[1])
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

            asyncio.create_task(_cleanup(msg))
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Failed building recruitment stats")
            await interaction.followup.send(
                "I couldn't build the recruitment stats right now. Try again in a moment.",
                ephemeral=True,
            )
