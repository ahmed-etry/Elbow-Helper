"""Discord command and interaction adapters for native rosters."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from datetime import timezone as dt_timezone
import logging
import re
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks

from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import warn
from elbow_helper.discord.timezones import build_timezone_choices
from elbow_helper.core.background import start_resilient_loop
from elbow_helper.configuration.clans import CLAN_ORDER
from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import HIBERNATING_ROLE_ID
from elbow_helper.configuration.roles import LEAD_PLUS
from elbow_helper.domain.timezones import canonical_timezone_name
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import LocalExportStore
from elbow_helper.infrastructure.exports import WorkbookWriter
from elbow_helper.infrastructure.time import fixed_utc_offset_name

from .services.accounts import RosterAccountDirectory
from .services.automation import RosterAutomationService
from .config import DEFAULT_MAX_MEMBERS
from .config import FAMILY_CLAN_CODE
from .config import MAX_ROSTER_MEMBERS
from .config import REFRESH_COOLDOWN_SECONDS
from .config import SCHEDULER_INTERVAL_SECONDS
from .repository import RosterRepository
from .services.membership import account_count
from .services.membership import RosterMembershipService
from .models import LinkedAccount
from .models import Roster
from .services.posts import message_page
from .services.posts import RosterPostService
from .services.profiles import RosterProfileService
from .services.publishing import RosterSheetPublisher
from .services.queries import RosterQueries
from .services.roles import RosterRoleSynchronizer
from .services.search import RosterSearchCache
from .services.service import RosterCapacityError
from .services.service import RosterDeleteCleanupError
from .services.service import RosterService
from .services.scheduling import due_window
from .services.scheduling import next_window
from .services.scheduling import normalize_clock
from .services.scheduling import one_off_window
from .services.scheduling import parse_day_rule
from .ui.views import AccountPickerView
from .ui.views import ConfirmClearView
from .ui.views import ConfirmDeleteView
from .ui.views import RosterLayoutView
from .ui.views import RosterProgressView
from .ui.views import ROSTER_LAYOUT_PROMPT
from .ui.views import roster_layout_columns_feedback
from .ui.views import roster_layout_lengths_feedback
from .ui.views import RosterRemovalView
from .ui.views import RosterSettingsView
from .ui.views import RosterTargetMemberView


LOGGER = logging.getLogger(__name__)
CLAN_CHOICES = [
    app_commands.Choice(name="Full clan family", value=FAMILY_CLAN_CODE),
    *[
        app_commands.Choice(name=f"{code} - {CLANS[code].name}", value=code)
        for code in CLAN_ORDER
    ],
]


def _is_roster_name_conflict(error: sqlite3.IntegrityError) -> bool:
    return "UNIQUE constraint failed: rosters.guild_id, rosters.name" in str(error)


def _unsupported_monthly_day(value: str) -> int | None:
    try:
        day = int(value.strip())
    except ValueError:
        return None
    return day if day in {29, 30, 31} else None


class Rosters(commands.Cog):
    """Account-level Discord rosters backed by AccountLinks."""

    def __init__(
        self,
        bot: commands.Bot,
        clash_client: ClashClient,
        google_publisher: GoogleSheetsPublisher,
        workbook_writer: WorkbookWriter,
        local_exports: LocalExportStore,
        repository: RosterRepository,
        account_directory: RosterAccountDirectory,
        role_synchronizer: RosterRoleSynchronizer,
    ):
        self.bot = bot
        self._repository = repository
        self._account_directory = account_directory
        self._roles = role_synchronizer
        self._locks: dict[int, asyncio.Lock] = {}
        self._refresh_times: dict[int, float] = {}
        self._membership_reconciled = False
        self._roster_search = RosterSearchCache(self._repository)
        self.queries = RosterQueries(self._repository)
        self.profiles = RosterProfileService(
            self._repository,
            clash_client,
        )
        self.posts = RosterPostService(
            self.bot,
            self._repository,
            clash_client,
            account_directory,
            self,
        )
        self.publisher = RosterSheetPublisher(
            self.bot,
            self._repository,
            self.profiles,
            google_publisher,
            workbook_writer,
            local_exports,
        )
        self.automation = RosterAutomationService(
            self.bot,
            self._repository,
            self._roles,
            self._lock,
            self.posts.refresh,
        )
        self.service = RosterService(
            self._repository,
            self._roster_search,
            self._roles,
            self.posts,
            self.automation,
        )
        self.membership = RosterMembershipService(
            self._repository,
            account_directory,
            clash_client,
            self._roles,
            self._lock,
            self.posts.refresh,
        )
        start_resilient_loop(self.scheduler_loop)

    async def cog_load(self) -> None:
        await self._get_roster_search().warm()
        await self.posts.restore_persistent_views()

    def cog_unload(self) -> None:
        self.scheduler_loop.cancel()
        search = getattr(self, "_roster_search", None)
        if search is not None:
            search.close()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.posts.refresh_posts_after_emoji_load()
        if not self._membership_reconciled:
            await self._reconcile_membership()

    @commands.Cog.listener()
    async def on_disconnect(self) -> None:
        self._membership_reconciled = False

    @commands.Cog.listener()
    async def on_guild_available(self, guild: discord.Guild) -> None:
        if self.bot.is_ready() and not self._membership_reconciled:
            await self._reconcile_membership()

    async def _reconcile_membership(self) -> None:
        reconciled = True
        for guild in self.bot.guilds:
            if guild.unavailable:
                reconciled = False
                continue
            if guild.chunked:
                members = guild.members
            else:
                try:
                    members = await guild.chunk(cache=True)
                except (discord.ClientException, asyncio.TimeoutError):
                    LOGGER.warning(
                        "Could not reconcile roster membership guild=%s",
                        guild.id,
                    )
                    reconciled = False
                    continue
            eligible_member_ids = {
                member.id
                for member in members
                if not any(
                    role.id == HIBERNATING_ROLE_ID for role in member.roles
                )
            }
            await self.membership.reconcile_eligible_members(
                guild.id,
                eligible_member_ids,
            )
        self._membership_reconciled = reconciled

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self.posts.remove_deleted_message(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self,
        payload: discord.RawBulkMessageDeleteEvent,
    ) -> None:
        await self.posts.remove_deleted_messages(payload.message_ids)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self.posts.remove_deleted_channel(channel.id)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        await self.posts.remove_deleted_channel(thread.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self.membership.remove_ineligible_member(member.guild.id, member.id)

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        was_hibernating = any(
            role.id == HIBERNATING_ROLE_ID for role in before.roles
        )
        is_hibernating = any(
            role.id == HIBERNATING_ROLE_ID for role in after.roles
        )
        if not was_hibernating and is_hibernating:
            await self.membership.remove_ineligible_member(after.guild.id, after.id)

    def _lock(self, roster_id: int) -> asyncio.Lock:
        return self._locks.setdefault(roster_id, asyncio.Lock())

    @staticmethod
    def is_lead(member: discord.abc.User) -> bool:
        return any(role.id in LEAD_PLUS for role in getattr(member, "roles", []))

    async def _require_lead(self, interaction: discord.Interaction) -> bool:
        if self.is_lead(interaction.user):
            return True
        await deny(interaction)
        return False

    async def roster_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        rows = await self._get_roster_search().rows(interaction.guild_id)
        needle = current.casefold().strip()
        return [
            app_commands.Choice(name=row.name[:100], value=str(row.id))
            for row in rows
            if not needle or needle in row.name.casefold() or needle == str(row.id)
        ][:25]

    def _get_roster_search(self) -> RosterSearchCache:
        search = getattr(self, "_roster_search", None)
        if search is None:
            search = RosterSearchCache(self._repository)
            self._roster_search = search
        return search

    async def timezone_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return build_timezone_choices(current)

    async def _resolve_roster(self, interaction: discord.Interaction, value: str) -> Roster | None:
        try:
            roster = await self.service.get(int(value))
        except (TypeError, ValueError):
            roster = None
        if roster and roster.guild_id == interaction.guild_id:
            return roster
        await warn(interaction, "That roster no longer exists.")
        return None

    @staticmethod
    def _clean_roster_name(value: str) -> str | None:
        cleaned = " ".join(value.split())
        return cleaned if 1 <= len(cleaned) <= 100 else None

    @staticmethod
    def validate_roster_name(value: str) -> str | None:
        return Rosters._clean_roster_name(value)

    async def roster_name_available(self, guild_id: int, name: str) -> bool:
        rows = await self.service.list_for_guild(guild_id)
        return all(row.name.casefold() != name.casefold() for row in rows)

    async def create_roster(self, *, guild_id: int, name: str,
                            clan_code: str, role_id: int | None,
                            max_members: int) -> Roster:
        return await self.service.create(
            guild_id=guild_id, name=name, clan_code=clan_code,
            role_id=role_id, max_members=max_members,
        )

    async def get_roster(self, roster_id: int) -> Roster | None:
        return await self.service.get(roster_id)

    async def post_roster(self, roster_id: int, send,
                          *, interaction: discord.Interaction | None = None,
                          rendered=None):
        async with self._lock(roster_id):
            current = await self.service.get(roster_id)
            if current is None:
                return None
            opened = await self.service.open(current)
            message = (await self.posts.post_interaction_response(opened, interaction)
                       if interaction is not None else
                       await self.posts.post(opened, send, rendered=rendered))
            return opened, message

    async def roster_post_registered(self, roster_id: int, message_id: int) -> bool:
        posts = await asyncio.to_thread(self._repository.list_posts, roster_id)
        return any(post.message_id == message_id for post in posts)

    async def roster_export_plan(self, roster: Roster) -> dict[str, object]:
        members = await self.service.list_members(roster)
        timestamp = datetime.now(dt_timezone.utc).strftime("%Y%m%d-%H%M%S")
        return {
            "roster": roster, "timestamp": timestamp,
            "workbook_name": self.publisher.workbook_name(roster.name, timestamp),
            "accounts": tuple(sorted((row.player_tag, row.discord_user_id)
                                     for row in members)),
        }

    async def export_roster(self, roster: Roster,
                            *, timestamp: str | None = None):
        if timestamp is None:
            return await self.publisher.export(roster)
        return await self.publisher.export(roster, timestamp=timestamp)

    async def deliver_roster_export(self, report) -> tuple[str | None, bytes | None]:
        try:
            if report.google_link:
                return report.google_link, None
            data = await asyncio.to_thread(report.workbook_path.read_bytes)
            return None, data
        finally:
            await self.publisher.discard(report)

    def roster_post_effect(self, roster: Roster,
                           *, now: datetime | None = None) -> dict[str, object]:
        now = now or datetime.now(dt_timezone.utc)
        if roster.one_off_open_ts is not None and roster.one_off_close_ts is not None:
            key = f"once:{roster.one_off_open_ts}"
            opens = (roster.one_off_open_ts <= int(now.timestamp()) < roster.one_off_close_ts
                     and roster.last_close_cycle_key != key)
            starts_cycle = (opens and (roster.last_open_cycle_key != key
                                       or not roster.active_cycle_id))
        elif roster.schedule_enabled:
            window = due_window(roster, now)
            key = window.cycle_key if window is not None else None
            opens = (window is not None and window.opens_at <= now < window.closes_at
                     and roster.last_close_cycle_key != key)
            starts_cycle = (opens and (roster.last_open_cycle_key != key
                                       or not roster.active_cycle_id))
        else:
            opens = True
            key = None
            starts_cycle = roster.active_cycle_id is None
        return {"opens": bool(opens), "starts_cycle": bool(starts_cycle),
                "clears_signups": bool(starts_cycle),
                "cycle_key": key if opens else None}

    async def preview_roster_post(self, roster: Roster) -> dict[str, object]:
        effect = self.roster_post_effect(roster)
        members = await self.service.list_members(roster)
        if effect["clears_signups"]:
            members = []
        projected = replace(
            roster, status="open" if effect["opens"] else "closed",
            last_open_cycle_key=(effect["cycle_key"]
                                 if effect["starts_cycle"] and effect["cycle_key"]
                                 else roster.last_open_cycle_key),
        )
        first = await self.posts.render(projected, members_override=members)
        pages = [first[0]]
        for page in range(1, first[2]):
            renders = await self.posts.render(
                projected, page=page, members_override=members,
            )
            pages.append(renders[0])
        return {"effect": effect, "rendered": first,
                "pages": tuple(tuple(embeds) for embeds in pages),
                "signature": tuple(tuple(repr(embed.to_dict()) for embed in embeds)
                                   for embeds in pages)}

    async def roster_deletion_state(self, roster: Roster) -> dict[str, object]:
        members = await self.service.list_members(roster)
        posts = await asyncio.to_thread(self._repository.list_posts, roster.id)
        history = []
        before_id = None
        while True:
            page = await asyncio.to_thread(
                self._repository.list_cycles, roster.guild_id, roster.id,
                before_id=before_id, limit=100,
            )
            if page is None:
                break
            for cycle in page.cycles:
                rows = await asyncio.to_thread(
                    self._repository.list_members, roster.id, cycle.id,
                )
                history.append((cycle.id, tuple(sorted(
                    (row.player_tag, row.discord_user_id) for row in rows
                ))))
            before_id = page.next_before_id
            if before_id is None:
                break
        return {
            "roster": roster,
            "member_ids": tuple(sorted({row.discord_user_id for row in members})),
            "posts": tuple(sorted((post.channel_id, post.message_id) for post in posts)),
            "history": tuple(history),
        }

    async def delete_roster(self, roster: Roster) -> None:
        await self.service.delete(roster)

    def roster_edit_changes(self, *, name: str | None,
                            clan_code: str | None, role_id: int | None,
                            max_members: int | None,
                            min_townhall: int | None,
                            remove_signup_role: bool) -> tuple[dict[str, object], str | None]:
        if role_id is not None and remove_signup_role:
            return {}, "Choose a signup role or remove it, not both."
        changes: dict[str, object] = {}
        if name is not None:
            clean_name = self.validate_roster_name(name)
            if clean_name is None:
                return {}, "Enter a roster name between 1 and 100 characters."
            changes["name"] = clean_name
        if clan_code is not None:
            changes["clan_code"] = clan_code
        if role_id is not None:
            changes["role_id"] = role_id
        elif remove_signup_role:
            changes["role_id"] = None
        if max_members is not None:
            changes["max_members"] = int(max_members)
        if min_townhall is not None and min_townhall > 0:
            changes["min_townhall"] = int(min_townhall)
        elif min_townhall == 0:
            changes["min_townhall"] = None
        if not changes:
            return {}, "Choose at least one roster setting to change."
        return changes, None

    async def roster_edit_state(self, roster: Roster) -> dict[str, object]:
        members = await self.service.list_members(roster)
        posts = await asyncio.to_thread(self._repository.list_posts, roster.id)
        return {
            "roster": roster,
            "account_count": len(members),
            "member_ids": tuple(sorted({row.discord_user_id for row in members})),
            "posts": tuple(sorted((post.channel_id, post.message_id) for post in posts)),
        }

    async def roster_management_state(self, roster_id: int) -> dict[str, object] | None:
        roster = await self.service.get(roster_id)
        return await self.roster_edit_state(roster) if roster is not None else None

    async def change_roster_management(self, roster_id: int, action: str) -> str:
        """Apply a roster management control and return its existing reply text."""
        if action not in {"open", "close", "toggle_buttons"}:
            raise ValueError("Unknown roster management action")
        async with self._lock(roster_id):
            roster = await self.service.get(roster_id)
            if roster is None:
                return "That roster no longer exists."
            if action == "open":
                roster = await self.service.open(roster)
                if roster.status == "open":
                    return f"Opened **{roster.name}**."
                if (roster.one_off_open_ts is not None
                        and int(time.time()) < roster.one_off_open_ts):
                    return (f"**{roster.name}** opens "
                            f"{discord.utils.format_dt(datetime.fromtimestamp(roster.one_off_open_ts, dt_timezone.utc))}.")
                return f"**{roster.name}** has passed its closing time."
            if action == "close":
                roster = await self.service.close(roster)
                return f"Closed **{roster.name}**."
            roster = await self.service.toggle_buttons(roster)
            return "Buttons shown." if not roster.buttons_hidden else "Buttons hidden."

    async def clear_roster_signups(self, roster_id: int):
        """Clear current signups for both the panel and agent."""
        return await self.membership.clear(roster_id)

    @staticmethod
    def roster_capacity_issue(current_count: int) -> str:
        return (f"This roster already has {account_count(current_count)} signed up. "
                f"Choose {current_count} or higher.")

    async def update_roster_settings(self, roster: Roster,
                                     changes: dict[str, object]) -> Roster:
        async with self._lock(roster.id):
            return await self.service.update(roster, changes)

    def plan_roster_timing(self, roster: Roster, *, opens_on: str | None,
                           closes_on: str | None, timezone: str | None,
                           reset_on_open: bool,
                           now: datetime | None = None) -> dict[str, object]:
        if opens_on is None and closes_on is None:
            return {"issue": None, "clear": True, "window": None,
                    "reset_on_open": reset_on_open, "now": now}
        if opens_on is None or closes_on is None:
            return {"issue": "Enter both opening and closing times, or leave both blank to clear them."}
        if roster.schedule_enabled:
            return {"issue": "Disable the automatic schedule before setting one-off timing."}
        canonical_tz = canonical_timezone_name(timezone or "")
        if canonical_tz is None:
            return {"issue": "Choose a timezone from the list."}
        window = one_off_window(
            opens_on=opens_on, closes_on=closes_on,
            timezone_name=canonical_tz,
        )
        if window is None:
            return {"issue": "Enter valid `YYYY-MM-DD HH:mm` times with the closing time after the opening time."}
        now = now or datetime.now(dt_timezone.utc)
        if window.closes_at <= now:
            return {"issue": "Closing time must be in the future."}
        return {"issue": None, "clear": False, "window": window,
                "reset_on_open": reset_on_open, "now": now}

    async def apply_roster_timing(self, roster: Roster,
                                  plan: dict[str, object]) -> Roster:
        if plan["clear"]:
            return await self.service.clear_one_off_timing(roster)
        return await self.service.set_one_off_timing(
            roster, plan["window"],
            reset_on_open=plan["reset_on_open"], now=plan["now"],
        )

    def plan_roster_schedule(self, roster: Roster, *,
                             open_day: str | None, open_time: str | None,
                             close_day: str | None, close_time: str | None,
                             timezone: str | None, enabled: bool,
                             reset_on_open: bool | None) -> dict[str, object]:
        changes: dict[str, object] = {"schedule_enabled": int(enabled)}
        normalized_open = parse_day_rule(open_day) if open_day is not None else None
        normalized_close = parse_day_rule(close_day) if close_day is not None else None
        normalized_open_time = normalize_clock(open_time) if open_time is not None else None
        normalized_close_time = normalize_clock(close_time) if close_time is not None else None
        canonical_tz = canonical_timezone_name(timezone) if timezone is not None else None
        fixed_timezone = fixed_utc_offset_name(canonical_tz) if canonical_tz is not None else None
        for supplied, normalized, label in (
            (open_day, normalized_open, "opening"),
            (close_day, normalized_close, "closing"),
        ):
            if supplied is not None and normalized is None:
                unsupported = _unsupported_monthly_day(supplied)
                if unsupported is not None:
                    return {"issue": (
                        f"Day {unsupported} isn't available every month. Use `last`, "
                        "`last-1`, or `last-2` for month-end timing."
                    )}
                return {"issue": (
                    f"Enter the {label} day as `1`–`28`, `last`, `last-1`, or `last-2`."
                )}
        if open_time is not None and normalized_open_time is None:
            return {"issue": "Enter the opening time in 24-hour `HH:mm` format."}
        if close_time is not None and normalized_close_time is None:
            return {"issue": "Enter the closing time in 24-hour `HH:mm` format."}
        if timezone is not None and canonical_tz is None:
            return {"issue": "Choose a timezone from the list."}
        for key, value in (
            ("open_day", normalized_open), ("close_day", normalized_close),
            ("open_time", normalized_open_time), ("close_time", normalized_close_time),
            ("schedule_utc_offset", fixed_timezone),
        ):
            if value is not None:
                changes[key] = value
        if reset_on_open is not None:
            changes["reset_on_open"] = int(reset_on_open)
        if not enabled:
            return {"issue": None, "enabled": False, "changes": changes,
                    "supplied_settings": len(changes) > 1}
        effective = {
            "open_day": normalized_open or parse_day_rule(roster.open_day or ""),
            "close_day": normalized_close or parse_day_rule(roster.close_day or ""),
            "open_time": normalized_open_time or normalize_clock(roster.open_time or ""),
            "close_time": normalized_close_time or normalize_clock(roster.close_time or ""),
            "timezone_name": fixed_timezone or canonical_timezone_name(roster.schedule_utc_offset or ""),
        }
        if not all(effective.values()):
            return {"issue": "Enter the opening day and time, closing day and time, and timezone."}
        if roster.one_off_open_ts is not None:
            return {"issue": "Clear the one-off timing before enabling automatic scheduling."}
        return {"issue": None, "enabled": True, "changes": changes,
                "effective": effective,
                "reset_on_open": (roster.reset_on_open if reset_on_open is None
                                  else reset_on_open)}

    async def apply_roster_schedule(self, roster: Roster,
                                    plan: dict[str, object],
                                    *, now: datetime | None = None) -> tuple[Roster, str]:
        if not plan["enabled"]:
            updated = await self.service.disable_schedule(roster, plan["changes"])
            if plan["supplied_settings"]:
                message = f"Saved the schedule for **{updated.name}**. Automatic scheduling is off."
            else:
                message = f"Disabled automatic scheduling for **{updated.name}**."
            if updated.status == "open":
                message += " The roster remains open."
            return updated, message
        now = now or datetime.now(dt_timezone.utc)
        effective = plan["effective"]
        updated = await self.service.configure_schedule(
            roster, timezone_name=str(effective["timezone_name"]),
            open_day=str(effective["open_day"]),
            open_time=str(effective["open_time"]),
            close_day=str(effective["close_day"]),
            close_time=str(effective["close_time"]),
            reset_on_open=plan["reset_on_open"], now=now,
        )
        message = f"Scheduled **{updated.name}**."
        display_window = due_window(updated, now)
        if not (updated.status == "open" and display_window is not None
                and display_window.opens_at <= now < display_window.closes_at):
            display_window = next_window(updated, now)
            window_label = "Next window"
        else:
            window_label = "Current window"
        if display_window is not None:
            message += (
                f"\n{window_label}: {discord.utils.format_dt(display_window.opens_at)} to "
                f"{discord.utils.format_dt(display_window.closes_at)}."
            )
        return updated, message

    def roster_schedule_preview(self, roster: Roster,
                                plan: dict[str, object],
                                *, now: datetime | None = None) -> dict[str, object]:
        if not plan["enabled"]:
            return {"enabled": False, "window": None,
                    "opens_now": False, "starts_cycle": False}
        now = now or datetime.now(dt_timezone.utc)
        effective = plan["effective"]
        candidate = replace(
            roster, schedule_enabled=True,
            schedule_utc_offset=effective["timezone_name"],
            open_day=effective["open_day"],
            open_time=effective["open_time"],
            close_day=effective["close_day"],
            close_time=effective["close_time"],
            reset_on_open=plan["reset_on_open"],
        )
        current = due_window(candidate, now)
        opens_now = (current is not None
                     and current.opens_at <= now < current.closes_at)
        starts_cycle = (opens_now and current.cycle_key != roster.last_open_cycle_key
                        and current.cycle_key != roster.last_close_cycle_key)
        return {"enabled": True,
                "window": current if opens_now else next_window(candidate, now),
                "opens_now": opens_now, "starts_cycle": starts_cycle}

    def roster_clone_settings(self, source: Roster, *, name: str,
                              clan_code: str | None, role_id: int | None,
                              max_members: int | None,
                              min_townhall: int | None) -> dict[str, object]:
        return {
            "source_id": source.id, "source_name": source.name,
            "name": name,
            "clan_code": clan_code if clan_code is not None else source.clan_code,
            "role_id": role_id if role_id is not None else source.role_id,
            "max_members": max_members if max_members is not None else source.max_members,
            "min_townhall": (source.min_townhall if min_townhall is None
                             else min_townhall or None),
            "buttons_hidden": source.buttons_hidden,
            "schedule_enabled": source.schedule_enabled,
            "schedule_utc_offset": source.schedule_utc_offset,
            "open_day": source.open_day, "open_time": source.open_time,
            "close_day": source.close_day, "close_time": source.close_time,
            "reset_on_open": source.reset_on_open,
        }

    async def clone_roster(self, source: Roster, *, name: str,
                           clan_code: str | None, role_id: int | None,
                           max_members: int | None,
                           min_townhall: int | None) -> Roster:
        return await self.service.clone(
            source, name=name, clan_code=clan_code, role_id=role_id,
            max_members=max_members, min_townhall=min_townhall,
        )

    async def handle_refresh(self, interaction: discord.Interaction, roster_id: int) -> None:
        status = await self.refresh_roster(roster_id, on_ready=interaction.response.defer)
        if status == "cooldown":
            await warn(interaction, "This roster was just refreshed. Try again in a moment.")
        elif status == "missing":
            await warn(interaction, "That roster no longer exists.")

    async def refresh_roster(self, roster_id: int, *, on_ready=None) -> str:
        """Refresh roster profiles, roles and post for its button and the agent."""
        now = time.monotonic()
        last = self._refresh_times.get(roster_id)
        if last is not None and now - last < REFRESH_COOLDOWN_SECONDS:
            return "cooldown"
        roster = await self.service.get(roster_id)
        if roster is None:
            return "missing"
        if on_ready is not None:
            await on_ready()
        self._refresh_times[roster_id] = now
        members = await self.service.list_members(roster)
        members = await self.profiles.refresh(roster, members)
        for member_id in {row.discord_user_id for row in members}:
            await self._roles.sync(
                roster,
                member_id,
                should_have=True,
            )
        await self.posts.refresh(roster)
        return "complete"

    async def handle_page(
        self,
        interaction: discord.Interaction,
        roster_id: int,
        action: str,
    ) -> None:
        roster = await self.service.get(roster_id)
        if roster is None:
            await warn(interaction, "That roster no longer exists.")
            return
        await interaction.response.defer()
        current_page = message_page(interaction.message, roster.id)
        if action == "first":
            page = 0
        elif action == "previous":
            page = current_page - 1
        elif action == "next":
            page = current_page + 1
        elif action == "last":
            page = None
        else:
            page = current_page
        embeds, page, page_count = await self.posts.render(roster, page)
        await interaction.edit_original_response(
            embeds=embeds,
            view=self.posts.message_view(
                roster,
                page=page,
                page_count=page_count,
            ),
        )

    async def show_account_picker(
        self,
        interaction: discord.Interaction,
        roster_id: int,
        *,
        mode: str,
        member_id: int | None = None,
        lead_override: bool = False,
        edit_response: bool = False,
    ) -> None:
        if edit_response:
            await interaction.response.edit_message(
                content=None,
                view=RosterProgressView("Loading accounts…"),
            )
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
        target_id = member_id or interaction.user.id
        result = await self.membership.account_picker(
            roster_id,
            target_id,
            mode=mode,
            for_other_member=lead_override,
        )
        if result.message is not None:
            view = (
                RosterTargetMemberView(self, roster_id, mode="add")
                if edit_response and result.return_to_member_picker
                else None
            )
            await interaction.edit_original_response(
                content=result.message,
                view=view,
            )
            return
        view = AccountPickerView(
            self,
            roster_id,
            member_id=target_id,
            accounts=list(result.accounts),
            mode=mode,
            lead_override=lead_override,
        )
        await interaction.edit_original_response(content=None, view=view)

    async def apply_account_selection(
        self,
        interaction: discord.Interaction,
        roster_id: int,
        *,
        member_id: int,
        player_tags: list[str],
        mode: str,
        account_snapshots: dict[str, LinkedAccount],
        bypass_min_townhall: bool = False,
    ) -> None:
        label = "Adding accounts…" if mode == "signup" else "Removing accounts…"
        await interaction.response.edit_message(
            content=None,
            view=RosterProgressView(label),
        )
        result = await self.change_roster_accounts(
            roster_id,
            member_id=member_id,
            player_tags=player_tags,
            mode=mode,
            account_snapshots=account_snapshots,
            bypass_min_townhall=bypass_min_townhall,
        )
        await interaction.edit_original_response(content=result.message, view=None)

    async def prepare_roster_account_selection(self, roster_id: int,
                                               member_id: int, *, mode: str,
                                               for_other_member: bool):
        """Return the accounts a roster picker would offer to this member."""
        roster = await self.service.get(roster_id)
        if roster is None:
            return None, None
        picker = await self.membership.account_picker(
            roster_id, member_id, mode=mode,
            for_other_member=for_other_member,
        )
        return roster, picker

    async def change_roster_accounts(self, roster_id: int, *, member_id: int,
                                     player_tags: list[str], mode: str,
                                     account_snapshots: dict[str, LinkedAccount],
                                     bypass_min_townhall: bool = False):
        """Apply an account selection for the panel and agent."""
        return await self.membership.apply_selection(
            roster_id, member_id=member_id, player_tags=player_tags,
            mode=mode, account_snapshots=account_snapshots,
            bypass_min_townhall=bypass_min_townhall,
        )

    @staticmethod
    def resolve_roster_account_choices(accounts: tuple[LinkedAccount, ...],
                                       requested: list[str]) -> tuple[list[str], str | None]:
        """Resolve names or tags exactly as one roster picker selection."""
        selected: list[str] = []
        for value in requested:
            tag = normalize_player_tag(value)
            matches = [account for account in accounts
                       if account.player_tag == tag
                       or account.player_name.casefold() == value.strip().casefold()]
            if len(matches) != 1:
                return [], ("More than one Clash account matches that name. Use a player tag."
                            if matches else "That Clash account isn't available for this roster.")
            if matches[0].player_tag not in selected:
                selected.append(matches[0].player_tag)
        return selected, None

    async def bulk_add_roster_accounts(
        self,
        interaction: discord.Interaction,
        roster_id: int,
        raw_tags: str,
    ) -> None:
        if not self.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.bulk_add_roster_tags(roster_id, raw_tags)
        await interaction.edit_original_response(content=result.message, view=None)

    async def bulk_add_roster_preview(self, roster_id: int,
                                      raw_tags: str) -> dict[str, object]:
        """Resolve every tag and affected member before a bulk add."""
        roster = await self.service.get(roster_id)
        if roster is None or roster.status != "open" or roster.active_cycle_id is None:
            raise ValueError("This roster is closed.")
        tags = []
        for value in re.split(r"[\s,;]+", raw_tags.strip()):
            if not value:
                continue
            tag = normalize_player_tag(value)
            if tag is None:
                raise ValueError("Enter valid player tags.")
            if tag not in tags:
                tags.append(tag)
        if not tags:
            raise ValueError("Enter at least one valid player tag.")
        signed = {row.player_tag for row in await self.service.list_members(roster)}
        rows = []
        for tag in tags:
            member_id = await asyncio.to_thread(self._account_directory.member_id_for_tag, tag)
            if member_id is None:
                raise ValueError(f"{tag} isn't linked to a member.")
            linked = await asyncio.to_thread(self._account_directory.for_member, member_id)
            account = next((row for row in linked if row.player_tag == tag), None)
            if account is None:
                raise ValueError(f"{tag} isn't linked to a member.")
            rows.append((tag, account.player_name, member_id, tag in signed))
        return {"roster": roster, "accounts": tuple(rows),
                "posts": (await self.roster_edit_state(roster))["posts"]}

    async def bulk_add_roster_tags(self, roster_id: int, raw_tags: str):
        """Add linked accounts for the panel and the agent."""
        return await self.membership.bulk_add(roster_id, raw_tags)

    async def show_settings(self, interaction: discord.Interaction, roster_id: int) -> None:
        roster = await self.service.get(roster_id)
        if roster is None:
            await warn(interaction, "That roster no longer exists.")
            return
        await interaction.response.send_message(
            view=RosterSettingsView(
                self,
                roster_id,
                is_open=roster.status == "open",
                buttons_hidden=roster.buttons_hidden,
            ),
            ephemeral=True,
        )

    async def show_roster_settings(
        self,
        interaction: discord.Interaction,
        roster_id: int,
    ) -> None:
        roster = await self.service.get(roster_id)
        if roster is None:
            await interaction.response.edit_message(
                content="That roster no longer exists.",
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=None,
            view=RosterSettingsView(
                self,
                roster.id,
                is_open=roster.status == "open",
                buttons_hidden=roster.buttons_hidden,
            ),
        )

    async def show_roster_layout(
        self,
        interaction: discord.Interaction,
        roster_id: int,
    ) -> None:
        roster = await self.service.get(roster_id)
        if roster is None:
            await interaction.response.edit_message(
                content="That roster no longer exists.",
                view=None,
            )
            return
        layout = await self.service.get_layout(roster.id)
        await interaction.response.edit_message(
            content=ROSTER_LAYOUT_PROMPT,
            view=RosterLayoutView(self, roster.id, layout),
        )

    async def roster_layout_state(self, roster_id: int):
        roster = await self.service.get(roster_id)
        if roster is None:
            return None
        return roster, await self.service.get_layout(roster_id)

    async def set_roster_layout(self, roster_id: int, **changes):
        """Apply layout changes for the panel and agent."""
        async with self._lock(roster_id):
            return await self.service.update_layout(roster_id, **changes)

    async def update_roster_layout_columns(
        self,
        interaction: discord.Interaction,
        roster_id: int,
        columns: set[str],
    ) -> None:
        if not self.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        await interaction.response.defer()
        roster, layout = await self.set_roster_layout(
            roster_id,
            show_townhall="townhall" in columns,
            show_discord="discord" in columns,
            show_clan="clan" in columns,
        )
        if roster is None:
            await interaction.edit_original_response(
                content="That roster no longer exists.", view=None,
            )
            return
        await interaction.edit_original_response(
            content=roster_layout_columns_feedback(layout),
            view=RosterLayoutView(self, roster.id, layout),
        )

    async def update_roster_layout_widths(
        self,
        interaction: discord.Interaction,
        roster_id: int,
        *,
        player_width: int,
        discord_width: int,
    ) -> None:
        if not self.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        await interaction.response.defer()
        roster, layout = await self.set_roster_layout(
            roster_id, player_width=player_width,
            discord_width=discord_width,
        )
        if roster is None:
            await interaction.edit_original_response(
                content="That roster no longer exists.", view=None,
            )
            return
        await interaction.edit_original_response(
            content=roster_layout_lengths_feedback(layout),
            view=RosterLayoutView(self, roster.id, layout),
        )

    async def show_roster_removal_picker(
        self,
        interaction: discord.Interaction,
        roster_id: int,
    ) -> None:
        roster = await self.service.get(roster_id)
        if roster is None:
            await interaction.response.edit_message(
                content="That roster no longer exists.",
                view=None,
            )
            return
        members = await self.service.list_members(roster)
        if not members:
            await interaction.response.edit_message(
                content="No signups to remove.",
                view=None,
            )
            return
        guild = self.bot.get_guild(roster.guild_id)
        display_names: dict[int, str] = {}
        if guild:
            for member in members:
                discord_member = guild.get_member(member.discord_user_id)
                if discord_member:
                    display_names[member.discord_user_id] = discord_member.display_name
        await interaction.response.edit_message(
            content=None,
            view=RosterRemovalView(
                self,
                roster.id,
                members,
                display_names,
            ),
        )

    async def remove_roster_players(
        self,
        interaction: discord.Interaction,
        roster_id: int,
        player_tags: list[str],
    ) -> None:
        if not self.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        await interaction.response.edit_message(
            content=None,
            view=RosterProgressView("Removing accounts…"),
        )
        result = await self.remove_roster_signup_rows(roster_id, player_tags)
        await interaction.edit_original_response(content=result.message, view=None)

    async def roster_signed_rows(self, roster_id: int):
        """Read all current signup rows for the removal panel and agent."""
        roster = await self.service.get(roster_id)
        if roster is None:
            return None
        members = await self.service.list_members(roster)
        return {"roster": roster, "members": tuple(members),
                "posts": (await self.roster_edit_state(roster))["posts"]}

    async def remove_roster_signup_rows(self, roster_id: int, player_tags: list[str]):
        """Remove selected signup rows for the panel and agent."""
        return await self.membership.remove_players(roster_id, player_tags)

    async def handle_management_action(
        self,
        interaction: discord.Interaction,
        roster_id: int,
        action: str,
    ) -> None:
        if not self.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        if action == "clear":
            await interaction.response.edit_message(
                content="Clear all current signups from this roster?",
                view=ConfirmClearView(self, roster_id),
            )
            return
        if action == "export":
            await interaction.response.edit_message(
                content=None,
                view=RosterProgressView("Exporting signups…"),
            )
        else:
            await interaction.response.defer()
        if action == "export":
            async with self._lock(roster_id):
                roster = await self.service.get(roster_id)
                if roster is None:
                    await interaction.edit_original_response(
                        content="That roster no longer exists.", view=None,
                    )
                    return
                await self._send_roster_export(interaction, roster)
            return
        text = (await self.change_roster_management(roster_id, action)
                if action in {"open", "close", "toggle_buttons"}
                else "That roster action isn't available.")
        await interaction.edit_original_response(content=text, view=None)

    async def confirm_clear(self, interaction: discord.Interaction, roster_id: int) -> None:
        await interaction.response.edit_message(
            content=None,
            view=RosterProgressView("Clearing signups…"),
        )
        result = await self.clear_roster_signups(roster_id)
        await interaction.edit_original_response(content=result.message, view=None)

    @staticmethod
    def _google_sheet_view(link: str) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Google Sheet",
                style=discord.ButtonStyle.link,
                url=link,
            )
        )
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", link)
        if match:
            view.add_item(
                discord.ui.Button(
                    label="Download",
                    style=discord.ButtonStyle.link,
                    url=(
                        "https://docs.google.com/spreadsheets/d/"
                        f"{match.group(1)}/export?format=xlsx"
                    ),
                )
            )
        return view

    async def _send_roster_export(
        self,
        interaction: discord.Interaction,
        roster: Roster,
    ) -> None:
        try:
            report, warning = await self.export_roster(roster)
        except (OSError, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Roster export failed roster_id=%s", roster.id)
            await interaction.edit_original_response(
                content="I couldn't create the roster spreadsheet.",
                view=None,
            )
            return
        if report is None:
            await interaction.edit_original_response(
                content=warning or "I couldn't create the roster spreadsheet.",
                view=None,
            )
            return

        delivered = False
        try:
            if report.google_link:
                await interaction.edit_original_response(
                    content=f"Exported **{roster.name}**.",
                    view=self._google_sheet_view(report.google_link),
                )
                delivered = True
                return

            lines = [f"Exported **{roster.name}**."]
            if report.google_warning:
                lines.append(report.google_warning)
            attachment = discord.File(
                str(report.workbook_path),
                filename=report.workbook_name,
            )
            try:
                message = await interaction.edit_original_response(
                    content="\n".join(lines),
                    attachments=[attachment],
                    view=None,
                )
            finally:
                attachment.close()
            delivered = bool(message.attachments)
            if message.attachments:
                view = discord.ui.View(timeout=None)
                view.add_item(
                    discord.ui.Button(
                        label="Download",
                        style=discord.ButtonStyle.link,
                        url=message.attachments[0].url,
                    )
                )
                await message.edit(view=view)
            else:
                await message.edit(
                    content="I couldn't deliver the roster spreadsheet.",
                    view=None,
                )
        finally:
            if delivered:
                await self.publisher.discard(report)

    @tasks.loop(seconds=SCHEDULER_INTERVAL_SECONDS)
    async def scheduler_loop(self) -> None:
        await self.automation.run_due(datetime.now(dt_timezone.utc))

    @scheduler_loop.before_loop
    async def before_scheduler_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self.posts.prune_stale()

    @app_commands.choices(clan=CLAN_CHOICES)
    @app_commands.describe(
        name="Name members see at the top of the roster.",
        clan="Show the roster for the full clan family or a single clan.",
        signup_role="Role given while a member has at least one account signed up.",
        max_members="Maximum Clash accounts; defaults to 500.",
    )
    async def roster_create(
        self,
        interaction: discord.Interaction,
        name: str,
        clan: app_commands.Choice[str],
        signup_role: discord.Role | None = None,
        max_members: app_commands.Range[int, 1, MAX_ROSTER_MEMBERS] = DEFAULT_MAX_MEMBERS,
    ) -> None:
        if not await self._require_lead(interaction):
            return
        if interaction.guild_id is None:
            await warn(interaction, "Run this command in the server.")
            return
        clean_name = self.validate_roster_name(name)
        if clean_name is None:
            await warn(interaction, "Enter a roster name between 1 and 100 characters.")
            return
        try:
            roster = await self.create_roster(
                guild_id=interaction.guild_id,
                name=clean_name,
                clan_code=clan.value,
                role_id=signup_role.id if signup_role else None,
                max_members=int(max_members),
            )
        except sqlite3.IntegrityError as error:
            if _is_roster_name_conflict(error):
                await warn(interaction, "A roster with that name already exists.")
            else:
                LOGGER.exception("Roster creation failed guild_id=%s", interaction.guild_id)
                await warn(interaction, "The roster couldn't be created.")
            return
        await interaction.response.send_message(
            f"Created **{roster.name}**.",
            ephemeral=True,
        )

    @app_commands.autocomplete(roster=roster_autocomplete)
    @app_commands.choices(clan=CLAN_CHOICES)
    @app_commands.describe(
        roster="Roster to update.",
        name="New name members see at the top of the roster.",
        clan="Show the roster for the full clan family or a single clan.",
        signup_role="Role given while a member has at least one account signed up.",
        max_members="Maximum number of Clash accounts that can sign up.",
        min_townhall="Minimum Town Hall for member signups; enter 0 for no minimum.",
        remove_signup_role="Remove the roster's current signup role.",
    )
    async def roster_edit(
        self,
        interaction: discord.Interaction,
        roster: str,
        name: str | None = None,
        clan: app_commands.Choice[str] | None = None,
        signup_role: discord.Role | None = None,
        max_members: app_commands.Range[int, 1, MAX_ROSTER_MEMBERS] | None = None,
        min_townhall: app_commands.Range[int, 0] | None = None,
        remove_signup_role: bool = False,
    ) -> None:
        if not await self._require_lead(interaction):
            return
        target = await self._resolve_roster(interaction, roster)
        if target is None:
            return
        changes, issue = self.roster_edit_changes(
            name=name, clan_code=clan.value if clan else None,
            role_id=signup_role.id if signup_role else None,
            max_members=max_members, min_townhall=min_townhall,
            remove_signup_role=remove_signup_role,
        )
        if issue:
            await warn(interaction, issue)
            return
        try:
            target = await self.update_roster_settings(target, changes)
        except RosterCapacityError as error:
            await warn(interaction, self.roster_capacity_issue(error.current_count))
            return
        except sqlite3.IntegrityError as error:
            if _is_roster_name_conflict(error):
                await warn(interaction, "A roster with that name already exists.")
            else:
                LOGGER.exception("Roster update failed roster_id=%s", target.id)
                await warn(interaction, "The roster couldn't be updated.")
            return
        await interaction.response.send_message(f"Updated **{target.name}**.", ephemeral=True)

    @app_commands.autocomplete(roster=roster_autocomplete, timezone=timezone_autocomplete)
    @app_commands.describe(
        roster="Roster whose opening and closing times you want to set.",
        opens_on="Opening date and time as YYYY-MM-DD HH:mm.",
        closes_on="Closing date and time as YYYY-MM-DD HH:mm.",
        timezone="Timezone for the opening and closing times.",
        reset_on_open="Clear existing signups when the roster opens.",
    )
    async def roster_timing(
        self,
        interaction: discord.Interaction,
        roster: str,
        opens_on: str | None = None,
        closes_on: str | None = None,
        timezone: str | None = None,
        reset_on_open: bool = True,
    ) -> None:
        if not await self._require_lead(interaction):
            return
        target = await self._resolve_roster(interaction, roster)
        if target is None:
            return
        plan = self.plan_roster_timing(
            target, opens_on=opens_on, closes_on=closes_on,
            timezone=timezone, reset_on_open=reset_on_open,
        )
        if plan["issue"]:
            await warn(interaction, plan["issue"])
            return
        if plan["clear"]:
            await interaction.response.defer(ephemeral=True)
            target = await self.apply_roster_timing(target, plan)
            await interaction.followup.send(
                f"Cleared one-off timing for **{target.name}**.",
                ephemeral=True,
            )
            return
        window = plan["window"]
        await interaction.response.defer(ephemeral=True)
        target = await self.apply_roster_timing(target, plan)
        await interaction.followup.send(
            f"Set **{target.name}** to open {discord.utils.format_dt(window.opens_at)} "
            f"and close {discord.utils.format_dt(window.closes_at)}.",
            ephemeral=True,
        )

    @app_commands.autocomplete(
        roster=roster_autocomplete,
        timezone=timezone_autocomplete,
    )
    @app_commands.describe(
        roster="Roster to schedule.",
        open_day="Opening day: 1–28, last, last-1, or last-2.",
        open_time="Opening time in 24-hour HH:mm format.",
        close_day="Closing day: 1–28, last, last-1, or last-2.",
        close_time="Closing time in 24-hour HH:mm format.",
        timezone="Timezone for the opening and closing times.",
        enabled="Enable or disable this monthly schedule.",
        reset_on_open="Clear existing signups each time the roster opens.",
    )
    async def roster_schedule(
        self,
        interaction: discord.Interaction,
        roster: str,
        open_day: str | None = None,
        open_time: str | None = None,
        close_day: str | None = None,
        close_time: str | None = None,
        timezone: str | None = None,
        enabled: bool = True,
        reset_on_open: bool | None = None,
    ) -> None:
        if not await self._require_lead(interaction):
            return
        target = await self._resolve_roster(interaction, roster)
        if target is None:
            return

        plan = self.plan_roster_schedule(
            target, open_day=open_day, open_time=open_time,
            close_day=close_day, close_time=close_time,
            timezone=timezone, enabled=enabled,
            reset_on_open=reset_on_open,
        )
        if plan["issue"]:
            await warn(interaction, plan["issue"])
            return
        await interaction.response.defer(ephemeral=True)
        _, message = await self.apply_roster_schedule(target, plan)
        await interaction.followup.send(message, ephemeral=True)

    @app_commands.autocomplete(roster=roster_autocomplete)
    @app_commands.describe(roster="Roster to post.")
    async def roster_post(self, interaction: discord.Interaction, roster: str) -> None:
        if not await self._require_lead(interaction):
            return
        target = await self._resolve_roster(interaction, roster)
        if target is None:
            return
        if interaction.channel is None:
            await warn(interaction, "Run this command in the channel where the roster should appear.")
            return
        await interaction.response.defer(thinking=True)
        result = await self.post_roster(
            target.id,
            lambda **kwargs: interaction.edit_original_response(
                content=None, **kwargs,
            ),
            interaction=interaction,
        )
        if result is None:
            await interaction.edit_original_response(
                content="That roster no longer exists.",
            )

    @app_commands.autocomplete(roster=roster_autocomplete)
    @app_commands.describe(roster="Roster whose current signups you want to export.")
    async def roster_export(self, interaction: discord.Interaction, roster: str) -> None:
        if not await self._require_lead(interaction):
            return
        target = await self._resolve_roster(interaction, roster)
        if target is None:
            return
        await interaction.response.defer(ephemeral=True)
        async with self._lock(target.id):
            current = await self.service.get(target.id)
            if current is None:
                await interaction.edit_original_response(content="That roster no longer exists.")
                return
            await self._send_roster_export(interaction, current)

    async def roster_list(self, interaction: discord.Interaction) -> None:
        if not await self._require_lead(interaction):
            return
        if interaction.guild_id is None:
            return
        rows = await self.service.list_for_guild(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("No rosters have been created.", ephemeral=True)
            return
        lines = []
        now = datetime.now(dt_timezone.utc)
        for row in rows:
            if row.one_off_open_ts is not None and row.one_off_close_ts is not None:
                opens_at = datetime.fromtimestamp(row.one_off_open_ts, dt_timezone.utc)
                closes_at = datetime.fromtimestamp(row.one_off_close_ts, dt_timezone.utc)
                schedule = (
                    f"One-off: {discord.utils.format_dt(opens_at)} to "
                    f"{discord.utils.format_dt(closes_at)}"
                )
            elif row.schedule_enabled:
                window = due_window(row, now)
                window_label = "Current"
                if window is None or not window.opens_at <= now < window.closes_at:
                    window = next_window(row, now)
                    window_label = "Next"
                schedule = (
                    f"{window_label}: {discord.utils.format_dt(window.opens_at)} to "
                    f"{discord.utils.format_dt(window.closes_at)}"
                    if window is not None
                    else "Schedule unavailable"
                )
            else:
                schedule = None
            minimum = (
                f", TH{row.min_townhall}+"
                if row.min_townhall is not None
                else ""
            )
            line = (
                f"- **{row.name}** — {row.status.title()} — "
                f"{account_count(row.max_members)} max{minimum}"
            )
            if schedule is not None:
                line += f" — {schedule}"
            lines.append(line)
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.autocomplete(roster=roster_autocomplete)
    @app_commands.choices(clan=CLAN_CHOICES)
    @app_commands.describe(
        roster="Roster to use as the starting point.",
        name="Name members should see on the new roster.",
        clan="Clan for the new roster. Leave empty to use the source roster's clan.",
        signup_role="Signup role for the new roster. Leave empty to use the source roster's role.",
        max_members="Account limit for the new roster. Leave empty to use the source roster's limit.",
        min_townhall="Town Hall minimum. Leave empty to use the source minimum; enter 0 for none.",
    )
    async def roster_clone(
        self,
        interaction: discord.Interaction,
        roster: str,
        name: str,
        clan: app_commands.Choice[str] | None = None,
        signup_role: discord.Role | None = None,
        max_members: app_commands.Range[int, 1, MAX_ROSTER_MEMBERS] | None = None,
        min_townhall: app_commands.Range[int, 0] | None = None,
    ) -> None:
        if not await self._require_lead(interaction):
            return
        source = await self._resolve_roster(interaction, roster)
        if source is None or interaction.guild_id is None:
            return
        clean_name = self.validate_roster_name(name)
        if clean_name is None:
            await warn(interaction, "Enter a roster name between 1 and 100 characters.")
            return
        try:
            clone = await self.clone_roster(
                source,
                name=clean_name,
                clan_code=clan.value if clan is not None else None,
                role_id=signup_role.id if signup_role is not None else None,
                max_members=int(max_members) if max_members is not None else None,
                min_townhall=(
                    int(min_townhall)
                    if min_townhall is not None
                    else None
                ),
            )
        except sqlite3.IntegrityError as error:
            if _is_roster_name_conflict(error):
                await warn(interaction, "A roster with that name already exists.")
            else:
                LOGGER.exception("Roster clone failed source_roster_id=%s", source.id)
                await warn(interaction, "The roster couldn't be created.")
            return
        await interaction.response.send_message(
            f"Created **{clone.name}** from **{source.name}**.",
            ephemeral=True,
        )

    @app_commands.autocomplete(roster=roster_autocomplete)
    @app_commands.describe(roster="Roster to permanently delete.")
    async def roster_delete(self, interaction: discord.Interaction, roster: str) -> None:
        if not await self._require_lead(interaction):
            return
        target = await self._resolve_roster(interaction, roster)
        if target is None:
            return
        await interaction.response.send_message(
            f"Delete **{target.name}** and all of its signup history?",
            view=ConfirmDeleteView(self, target.id),
            ephemeral=True,
        )

    async def confirm_delete(self, interaction: discord.Interaction, roster_id: int) -> None:
        await interaction.response.defer()
        async with self._lock(roster_id):
            roster = await self.service.get(roster_id)
            if roster is None:
                await interaction.edit_original_response(
                    content="That roster no longer exists.",
                    view=None,
                )
                return
            try:
                await self.delete_roster(roster)
            except RosterDeleteCleanupError as error:
                LOGGER.warning(
                    "Roster deletion cleanup incomplete roster_id=%s members=%s messages=%s",
                    roster.id,
                    error.member_ids,
                    error.message_ids,
                )
                await interaction.edit_original_response(
                    content=(
                        f"**{roster.name}** was not deleted because one or more "
                        "signup roles or roster posts could not be removed. Try again."
                    ),
                    view=None,
                )
                return
        await interaction.edit_original_response(
            content=f"Deleted **{roster.name}**.",
            view=None,
        )
