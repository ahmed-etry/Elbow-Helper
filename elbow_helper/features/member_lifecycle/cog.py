from __future__ import annotations

import asyncio
from collections.abc import Coroutine
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from elbow_helper.core.background import start_resilient_loop
from elbow_helper.configuration.channels import OVERSEEING_TERRACE, TICKETS_LOG
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import APPLICANT_ROLE_ID, HIBERNATING_ROLE_ID
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from .config import CWL_ROLE_IDS, HIBERNATION_CLAN_ROLES, WAR_ROLE_IDS
from .contracts import HibernationReader
from .helpers import (
    account_age_str,
    find_home_clan_name,
    human_timedelta,
    platform_from_inviter,
    roles_intersection,
    snapshot_invites,
)
from .reports import ReportsMixin
from .queries import LifecycleRecordSource, MemberLifecycleQueries
from .state import LifecycleState, WORKFLOW, joined_at_us
from .tickets import TicketIndexMixin
from .views import ApplicantCleanupView

LOGGER = logging.getLogger(__name__)


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


    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not member.guild or member.guild.id != GUILD_ID:
            return

        overseer = member.guild.get_channel(OVERSEEING_TERRACE)
        if not isinstance(overseer, discord.TextChannel):
            LOGGER.warning("Overseer channel not found: %s", OVERSEEING_TERRACE)
            return

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

        acct_age, _ = account_age_str(member)
        embed = discord.Embed(
            title="🛰️ Joined Server",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(name="👤 Discord Member", value=f"{member.display_name} • {member.mention}", inline=False)
        embed.add_field(name="🕒 Account Age", value=acct_age, inline=True)
        embed.add_field(name="🌍 Platform", value=platform, inline=True)

        message_id = None
        try:
            message_id = (await overseer.send(embed=embed)).id
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Failed to send join snapshot: %s", exc)
        # The join's time is the record's; this keeps its source, counted for the week as before.
        await self.lifecycle_state.joined(member.id, joined_at_us(member), platform, message_id)

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
            is_applicant = not is_hibernating and any(role.id == APPLICANT_ROLE_ID for role in member.roles)

            # Discord's join time for this stay, the one the family record keeps.
            joined_at = member.joined_at or datetime.now(timezone.utc)

            duration = human_timedelta(joined_at, datetime.now(timezone.utc))
            home_clan = find_home_clan_name(member) or "None"
            war_roles = ", ".join(roles_intersection(member, WAR_ROLE_IDS)) or "None"
            cwl_roles = ", ".join(roles_intersection(member, CWL_ROLE_IDS)) or "None"
            reason_text = "Left voluntarily"

            try:
                async for entry in member.guild.audit_logs(limit=10, action=discord.AuditLogAction.kick):
                    if entry.target.id == member.id and (datetime.now(timezone.utc) - entry.created_at).total_seconds() < 120:
                        reason_text = f"Kicked by {entry.user}"
                        reason = "kicked"
                        break
                else:
                    async for entry in member.guild.audit_logs(limit=10, action=discord.AuditLogAction.ban):
                        if entry.target.id == member.id and (datetime.now(timezone.utc) - entry.created_at).total_seconds() < 120:
                            reason_text = f"Banned by {entry.user}"
                            reason = "banned"
                            break
            except (discord.Forbidden, discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                LOGGER.warning("Audit log read failed: %s", exc)

            embed = discord.Embed(
                title="🛰️ Left Server",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
            embed.add_field(name="👤 Discord Member", value=f"{member.nick or member.display_name} • {member.mention}", inline=False)

            ticket_links, total_ticket_matches = await self._find_ticket_log_links_for_member(member.guild, member.id)
            if total_ticket_matches > 0:
                superscript_digits = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")
                if len(ticket_links) == 1:
                    link_lines = [f"[Jump!]({ticket_links[0]})"]
                else:
                    link_lines = [
                        f"[Jump!{str(index).translate(superscript_digits)}]({url})"
                        for index, url in enumerate(ticket_links, start=1)
                    ]
                extra = total_ticket_matches - len(ticket_links)
                if extra > 0:
                    noun = "ticket log" if extra == 1 else "ticket logs"
                    link_lines.append(f"+{extra} more {noun}")
                field_name = "📁 Ticket Log" if total_ticket_matches == 1 else "📁 Ticket Logs"
                embed.add_field(name=field_name, value="\n".join(link_lines), inline=False)

            if is_hibernating:
                embed.add_field(name="Status", value="Hibernating", inline=True)
                hib_entry = hibernation_entry or {}
                saved_roles = hib_entry.get("roles", []) + hib_entry.get("rank_roles", [])
                fixed_roles: list[int] = []
                for role_id in saved_roles:
                    try:
                        fixed_roles.append(int(role_id))
                    except (TypeError, ValueError):
                        continue
                clan_name = "None"
                for role_id in fixed_roles:
                    if role_id in HIBERNATION_CLAN_ROLES:
                        clan_name = HIBERNATION_CLAN_ROLES[role_id]
                        break
                embed.add_field(name="🛡️ Clan Status", value=clan_name, inline=True)
                embed.add_field(name="📅 Time in Server", value=duration, inline=True)
                embed.add_field(name="🎯 Reason", value=reason_text, inline=False)
            elif is_applicant:
                embed.add_field(name="Status", value="Applicant", inline=True)
                embed.add_field(name="🎯 Reason", value=reason_text, inline=False)
            else:
                embed.add_field(name="Status", value="Member", inline=True)
                embed.add_field(name="🛡️ Clan Status", value=home_clan, inline=True)
                embed.add_field(name="📅 Time in Server", value=duration, inline=True)
                embed.add_field(name="⚔️ War Status", value=war_roles, inline=False)
                embed.add_field(name="🏅 CWL Status", value=cwl_roles, inline=False)
                embed.add_field(name="🎯 Reason", value=reason_text, inline=False)

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
            # The leave's time is the record's; this keeps why, and the week's source count as before.
            await self.lifecycle_state.left(member.id, joined_at_us(member), reason, message_id)
