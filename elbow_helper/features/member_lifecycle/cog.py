from __future__ import annotations

import asyncio
from collections.abc import Coroutine
import logging
import random
import sqlite3
from datetime import datetime, timezone
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from elbow_helper.core.background import start_resilient_loop
from elbow_helper.configuration.channels import OVERSEEING_TERRACE, TICKETS_LOG
from elbow_helper.configuration.clans import CLAN_MEMBER_ROLE_IDS
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import APPLICANT_ROLE_ID, HIBERNATING_ROLE_ID, MEMBER_ROLE_ID, TRIAL_ROLE_ID
from elbow_helper.features.hibernation.config import SNAPSHOT_ONLY_ROLE_IDS
from elbow_helper.infrastructure.discord.application_emojis import get_application_emoji_provider
from elbow_helper.infrastructure.discord.membership_history import saved_clan_and_rank_ids

from .config import CWL_ROLE_IDS, HOME_CLAN_ROLE_IDS, LEAVE_PHRASES, WAR_ROLE_IDS
from .contracts import HibernationReader
from .helpers import (
    account_age, duration, platform_from_inviter, reason_value, role_lines, snapshot_invites,
    ticket_log_value,
)
from .reports import ReportsMixin
from .queries import LifecycleRecordSource, MemberLifecycleQueries
from .state import LifecycleState, WORKFLOW, joined_at_us
from .tickets import TicketIndexMixin
from .views import ApplicantCleanupView
from .weeks import WEEK, month_bounds, ordinal, week_start

LOGGER = logging.getLogger(__name__)


def _role_ids(values: Any) -> list[int]:
    ids: list[int] = []
    for value in values or ():
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


class MemberLifecycle(commands.Cog, TicketIndexMixin, ReportsMixin):
    """Member join/leave auditing and recruitment reporting."""

    def __init__(
        self,
        bot: commands.Bot,
        hibernation_reader: HibernationReader,
    ):
        self.bot = bot
        self.hibernation_reader = hibernation_reader
        self.lifecycle_state = LifecycleState(bot.workflow_events)
        self.queries = MemberLifecycleQueries(
            self.lifecycle_state.snapshot,
            source=LifecycleRecordSource(self.lifecycle_state, lambda: getattr(bot, "people_reads", None)),
        )
        self._ticket_index_lock = asyncio.Lock()
        self._ticket_index_task: asyncio.Task | None = None
        self._report_cleanup_tasks: set[asyncio.Task[None]] = set()
        self.invite_cache: dict[str, dict[str, Any]] = {}

    @property
    def state(self) -> dict[str, Any]:
        """The saved state; change it only through ``lifecycle_state``."""
        return self.lifecycle_state.state

    async def cog_load(self):
        await self.lifecycle_state.startup()
        start_resilient_loop(self.weekly_report)
        start_resilient_loop(self.applicant_linger_scan)

    async def _refresh_invite_cache(self, guild: discord.Guild):
        self.invite_cache = await snapshot_invites(guild)

    def _start_report_cleanup(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        message_id: int,
    ) -> None:
        task = asyncio.create_task(
            coroutine,
            name=f"recruitment-stats-cleanup:{message_id}",
        )
        self._report_cleanup_tasks.add(task)

        def finish(completed: asyncio.Task[None]) -> None:
            self._report_cleanup_tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                return
            except Exception:
                LOGGER.exception(
                    "Recruitment stats cleanup failed for message %s",
                    message_id,
                )

        task.add_done_callback(finish)

    @commands.Cog.listener()
    async def on_ready(self):
        try:
            guild = self.bot.get_guild(GUILD_ID)
            if guild:
                await self._refresh_invite_cache(guild)
                if self._ticket_index_task is None or self._ticket_index_task.done():
                    self._ticket_index_task = asyncio.create_task(self._refresh_ticket_log_index(guild))

            for message_id, report in self.state.get("applicant_reports", {}).items():
                if not report.get("active"):
                    continue
                try:
                    self.bot.add_view(ApplicantCleanupView(self, int(message_id)), message_id=int(message_id))
                except (ValueError, discord.HTTPException):
                    LOGGER.warning("Failed to restore cleanup view for %s", message_id)
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Failed to prime snapshot intel on_ready")

    def cog_unload(self):
        self.weekly_report.cancel()
        self.applicant_linger_scan.cancel()
        if self._ticket_index_task and not self._ticket_index_task.done():
            self._ticket_index_task.cancel()
        for task in tuple(self._report_cleanup_tasks):
            if not task.done():
                task.cancel()
        self.lifecycle_state.events.unregister_member_deletion(WORKFLOW)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Last messages are the Discord collector's; a message only updates the ticket-log index.
        if not message.guild or message.author.bot:
            return
        if message.guild.id == GUILD_ID and message.channel.id == TICKETS_LOG:
            await self._index_ticket_log_message(message)


    async def _application_emoji(self, name: str) -> str | None:
        if not callable(getattr(self.bot, "fetch_application_emojis", None)):
            return None
        catalog = await get_application_emoji_provider(self.bot).get(required_names=(name,))
        return catalog.get(name)

    async def _emoji_thumbnail_url(self, name: str) -> str | None:
        token = await self._application_emoji(name)
        if not token:
            return None
        emoji_id = discord.PartialEmoji.from_str(token).id
        return f"https://cdn.discordapp.com/emojis/{emoji_id}.webp?animated=true"

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not member.guild or member.guild.id != GUILD_ID:
            return

        overseer = member.guild.get_channel(OVERSEEING_TERRACE)
        if not isinstance(overseer, discord.TextChannel):
            LOGGER.warning("Overseer channel not found: %s", OVERSEEING_TERRACE)
            overseer = None

        if not self.invite_cache:
            await self._refresh_invite_cache(member.guild)

        inviter_name = None
        try:
            current_invites = await snapshot_invites(member.guild)
            for code, info in current_invites.items():
                prev_uses = self.invite_cache.get(code, {}).get("uses", 0)
                curr_uses = info.get("uses", 0)
                if curr_uses > prev_uses:
                    inviter_name = info.get("inviter")
                    break
            if inviter_name is None:
                for code, info in self.invite_cache.items():
                    if code not in current_invites and info.get("uses", 0) >= 0:
                        inviter_name = info.get("inviter")
                        break
            self.invite_cache = current_invites
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Invite diff failed: %s", exc)

        platform = platform_from_inviter(inviter_name)

        embed = discord.Embed(title="Joined Server", color=discord.Color(0x29B15F))
        embed.set_author(
            name=member.nick or member.display_name,
            icon_url=member.display_avatar.replace(size=64).url,
        )
        thumbnail_url = await self._emoji_thumbnail_url("join_hi")
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        embed.add_field(name="Member", value=member.mention + "\n\u200b", inline=False)
        embed.add_field(
            name="Account Age",
            value=f"🎂 {account_age(member.created_at, datetime.now(timezone.utc))}",
            inline=False,
        )
        embed.add_field(name="Platform", value=f"🪧 {platform}\n\u200b", inline=False)
        now = datetime.now(timezone.utc)
        this_week = week_start(now)
        month_from, month_until = month_bounds(now)
        # This member's own join is recorded after the post, so they count here unless already counted.
        in_week = await self.lifecycle_state.stayed_joins(this_week, this_week + WEEK)
        in_month = await self.lifecycle_state.stayed_joins(month_from, month_until)
        week_place = len(in_week) + (member.id not in in_week)
        month_place = len(in_month) + (member.id not in in_month)
        embed.add_field(name=f"{now:%B %Y}", value=f"📅 {ordinal(month_place)} to join", inline=False)
        embed.add_field(name="Since Monday", value=f"👥 {ordinal(week_place)} to join", inline=False)

        message_id = None
        if overseer is not None:
            try:
                message_id = (await overseer.send(embed=embed)).id
            except (discord.Forbidden, discord.HTTPException) as exc:
                LOGGER.warning("Failed to send join snapshot: %s", exc)
        # The join's time is the record's; this keeps its source.
        await self.lifecycle_state.joined(member.id, joined_at_us(member), platform, message_id)

    async def _days_on_trial(self, member_id: int) -> int | None:
        try:
            started = await self.lifecycle_state.events.read(
                workflow="recruitment", kind="trial_started", subject=member_id)
        except sqlite3.Error as exc:
            LOGGER.warning("Trial start read failed: %s", exc)
            return None
        if not started:
            return None
        now = datetime.now(timezone.utc).timestamp()
        return max(0, int((now - started[-1]["occurred_at"] / 1_000_000) // 86400))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not member.guild or member.guild.id != GUILD_ID:
            return

        reason = "left"
        message_id = None
        try:
            overseer = member.guild.get_channel(OVERSEEING_TERRACE)
            if not isinstance(overseer, discord.TextChannel):
                LOGGER.warning("Overseer channel not found: %s", OVERSEEING_TERRACE)
                return

            hibernation_entry = self.hibernation_reader.get_member(member.id)
            is_hibernating = (
                any(role.id == HIBERNATING_ROLE_ID for role in member.roles)
                or hibernation_entry is not None
            )
            held = {role.id for role in member.roles}
            status = (
                "Hibernating" if is_hibernating
                else "Applicant" if APPLICANT_ROLE_ID in held
                else "Trial" if TRIAL_ROLE_ID in held
                else "Member"
            )

            reason_icon, reason_text = "🍃", None

            def actor_name(user) -> str:
                # The server nickname or display name, not the account handle.
                if user is None:
                    return str(user)
                return (member.guild.get_member(user.id) or user).display_name

            try:
                async for entry in member.guild.audit_logs(limit=10, action=discord.AuditLogAction.kick):
                    if entry.target.id == member.id and (datetime.now(timezone.utc) - entry.created_at).total_seconds() < 120:
                        reason_icon, reason_text = "🥾", f"Kicked by {actor_name(entry.user)}"
                        reason = "kicked"
                        break
                else:
                    async for entry in member.guild.audit_logs(limit=10, action=discord.AuditLogAction.ban):
                        if entry.target.id == member.id and (datetime.now(timezone.utc) - entry.created_at).total_seconds() < 120:
                            reason_icon, reason_text = "⚖️", f"Banned by {actor_name(entry.user)}"
                            reason = "banned"
                            break
            except (discord.Forbidden, discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                LOGGER.warning("Audit log read failed: %s", exc)

            embed = discord.Embed(title="Left Server", color=discord.Color(0xD10A0A))
            embed.set_author(
                name=member.nick or member.display_name,
                icon_url=member.display_avatar.replace(size=64).url,
            )
            thumbnail_url = await self._emoji_thumbnail_url("leave_bye")
            if thumbnail_url:
                embed.set_thumbnail(url=thumbnail_url)

            now = datetime.now(timezone.utc).timestamp()
            fields = [("Member", member.mention + "\n\u200b")]
            if member.joined_at is not None:
                fields.append(("Time in Server", f"🏕️ {duration(now - member.joined_at.timestamp())}"))
            status_role_id = {
                "Member": MEMBER_ROLE_ID, "Trial": TRIAL_ROLE_ID,
                "Applicant": APPLICANT_ROLE_ID, "Hibernating": HIBERNATING_ROLE_ID,
            }[status]
            fields.append(("Status", f"<@&{status_role_id}>"))
            if status == "Trial":
                days = await self._days_on_trial(member.id)
                if days is not None:
                    lasted = f"{days} day{'' if days == 1 else 's'}" if days else "Less than a day"
                    fields.append(("Lasted", f"📆 {lasted}"))
            reason_index = len(fields)
            blank = await self._application_emoji("blank")
            reason_text = reason_text or random.choice(LEAVE_PHRASES)
            fields.append(("Reason", reason_value(reason_icon, reason_text, blank)))

            def add_roles(name: str, role_ids: list[int]) -> None:
                if role_ids:
                    value = role_lines(member.guild.get_role(role_id) for role_id in role_ids)
                    fields.append((name if len(role_ids) == 1 else f"{name}s", value))

            if status in ("Member", "Trial"):
                roles = {role.id: role for role in member.roles}
                for name, role_ids in (
                    ("Clan", HOME_CLAN_ROLE_IDS), ("War", WAR_ROLE_IDS), ("CWL", CWL_ROLE_IDS),
                ):
                    value = role_lines(roles[role_id] for role_id in role_ids if role_id in roles)
                    if value:
                        fields.append((name, value))
                # Split as a hibernation saves them, so both name the same ranks.
                _, rank_ids = saved_clan_and_rank_ids(
                    member.guild,
                    [role_id for role_id in roles if role_id not in SNAPSHOT_ONLY_ROLE_IDS],
                    [role_id for role_id in roles if role_id in SNAPSHOT_ONLY_ROLE_IDS],
                )
                add_roles("Rank", rank_ids)
            elif status == "Hibernating":
                saved = hibernation_entry or {}
                # A finished hibernation keeps only its case start, in microseconds.
                since = (saved.get("hibernation_progress") or {}).get("unix_ts")
                if not since and saved.get("_case_started_at"):
                    since = saved["_case_started_at"] / 1_000_000
                if since:
                    fields.append(("Hibernated For", f"🗓️ {duration(now - since)}"))
                clan_ids, rank_ids = saved_clan_and_rank_ids(
                    member.guild, _role_ids(saved.get("roles")), _role_ids(saved.get("rank_roles")))
                # Same order as a current member's clans.
                clan_ids = [role_id for role_id in CLAN_MEMBER_ROLE_IDS.values() if role_id in clan_ids]
                add_roles("Clan", clan_ids)
                add_roles("Rank", rank_ids)

            ticket_links = await self._find_ticket_log_links_for_member(member.guild, member.id)
            if ticket_links:
                field_name = "Ticket Log" if len(ticket_links) == 1 else "Ticket Logs"
                fields.append((field_name, ticket_log_value(ticket_links, blank)))

            if len(fields) > reason_index + 1:
                fields[reason_index] = ("Reason", fields[reason_index][1] + "\n\u200b")
            for name, value in fields:
                embed.add_field(name=name, value=value, inline=False)

            message_id = (await overseer.send(embed=embed)).id
        except (
            discord.Forbidden,
            discord.HTTPException,
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            KeyError,
            AttributeError,
        ):
            LOGGER.exception("Unexpected error building/sending leave snapshot for %s", member)
        finally:
            # The leave's time is the record's; this keeps why.
            await self.lifecycle_state.left(member.id, joined_at_us(member), reason, message_id)
