"""Discord command and interaction adapters for native rosters."""

from __future__ import annotations

import asyncio
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
from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.clans import CLAN_ORDER
from elbow_helper.configuration.roles import LEAD_PLUS
from elbow_helper.domain.timezones import canonical_timezone_name
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
        self._roles = role_synchronizer
        self._locks: dict[int, asyncio.Lock] = {}
        self._refresh_times: dict[int, float] = {}
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
        self.scheduler_loop.start()

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

    async def handle_refresh(self, interaction: discord.Interaction, roster_id: int) -> None:
        now = time.monotonic()
        last = self._refresh_times.get(roster_id, 0.0)
        if now - last < REFRESH_COOLDOWN_SECONDS:
            await warn(interaction, "This roster was just refreshed. Try again in a moment.")
            return
        roster = await self.service.get(roster_id)
        if roster is None:
            await warn(interaction, "That roster no longer exists.")
            return
        await interaction.response.defer()
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
        result = await self.membership.apply_selection(
            roster_id,
            member_id=member_id,
            player_tags=player_tags,
            mode=mode,
            account_snapshots=account_snapshots,
            bypass_min_townhall=bypass_min_townhall,
        )
        await interaction.edit_original_response(content=result.message, view=None)

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
        result = await self.membership.bulk_add(roster_id, raw_tags)
        await interaction.edit_original_response(content=result.message, view=None)

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
        async with self._lock(roster_id):
            roster, layout = await self.service.update_layout(
                roster_id,
                show_townhall="townhall" in columns,
                show_discord="discord" in columns,
                show_clan="clan" in columns,
            )
            if roster is None:
                await interaction.edit_original_response(
                    content="That roster no longer exists.",
                    view=None,
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
        async with self._lock(roster_id):
            roster, layout = await self.service.update_layout(
                roster_id,
                player_width=player_width,
                discord_width=discord_width,
            )
            if roster is None:
                await interaction.edit_original_response(
                    content="That roster no longer exists.",
                    view=None,
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
        result = await self.membership.remove_players(roster_id, player_tags)
        await interaction.edit_original_response(content=result.message, view=None)

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
        async with self._lock(roster_id):
            roster = await self.service.get(roster_id)
            if roster is None:
                await interaction.edit_original_response(
                    content="That roster no longer exists.",
                    view=None,
                )
                return
            if action == "open":
                roster = await self.service.open(roster)
                if roster.status == "open":
                    text = f"Opened **{roster.name}**."
                elif (
                    roster.one_off_open_ts is not None
                    and int(time.time()) < roster.one_off_open_ts
                ):
                    text = (
                        f"**{roster.name}** opens "
                        f"{discord.utils.format_dt(datetime.fromtimestamp(roster.one_off_open_ts, dt_timezone.utc))}."
                    )
                else:
                    text = f"**{roster.name}** has passed its closing time."
            elif action == "close":
                roster = await self.service.close(roster)
                text = f"Closed **{roster.name}**."
            elif action == "export":
                await self._send_roster_export(interaction, roster)
                return
            elif action == "toggle_buttons":
                roster = await self.service.toggle_buttons(roster)
                text = "Buttons shown." if not roster.buttons_hidden else "Buttons hidden."
            else:
                text = "That roster action isn't available."
            await interaction.edit_original_response(content=text, view=None)

    async def confirm_clear(self, interaction: discord.Interaction, roster_id: int) -> None:
        await interaction.response.edit_message(
            content=None,
            view=RosterProgressView("Clearing signups…"),
        )
        result = await self.membership.clear(roster_id)
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
            report, warning = await self.publisher.export(roster)
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
        clean_name = self._clean_roster_name(name)
        if clean_name is None:
            await warn(interaction, "Enter a roster name between 1 and 100 characters.")
            return
        try:
            roster = await self.service.create(
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
        if signup_role and remove_signup_role:
            await warn(interaction, "Choose a signup role or remove it, not both.")
            return
        changes: dict[str, object] = {}
        if name is not None:
            clean_name = self._clean_roster_name(name)
            if clean_name is None:
                await warn(interaction, "Enter a roster name between 1 and 100 characters.")
                return
            changes["name"] = clean_name
        if clan is not None:
            changes["clan_code"] = clan.value
        if signup_role is not None:
            changes["role_id"] = signup_role.id
        elif remove_signup_role:
            changes["role_id"] = None
        if max_members is not None:
            changes["max_members"] = int(max_members)
        if min_townhall is not None and min_townhall > 0:
            changes["min_townhall"] = int(min_townhall)
        elif min_townhall == 0:
            changes["min_townhall"] = None
        if not changes:
            await warn(interaction, "Choose at least one roster setting to change.")
            return
        try:
            async with self._lock(target.id):
                target = await self.service.update(target, changes)
        except RosterCapacityError as error:
            await warn(
                interaction,
                f"This roster already has {account_count(error.current_count)} signed up. "
                f"Choose {error.current_count} or higher.",
            )
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
        if opens_on is None and closes_on is None:
            await interaction.response.defer(ephemeral=True)
            target = await self.service.clear_one_off_timing(target)
            await interaction.followup.send(
                f"Cleared one-off timing for **{target.name}**.",
                ephemeral=True,
            )
            return
        if opens_on is None or closes_on is None:
            await warn(
                interaction,
                "Enter both opening and closing times, or leave both blank to clear them.",
            )
            return
        if target.schedule_enabled:
            await warn(
                interaction,
                "Disable the automatic schedule before setting one-off timing.",
            )
            return
        canonical_tz = canonical_timezone_name(timezone or "")
        if canonical_tz is None:
            await warn(interaction, "Choose a timezone from the list.")
            return
        window = one_off_window(
            opens_on=opens_on,
            closes_on=closes_on,
            timezone_name=canonical_tz,
        )
        if window is None:
            await warn(
                interaction,
                "Enter valid `YYYY-MM-DD HH:mm` times with the closing time after the opening time.",
            )
            return
        now = datetime.now(dt_timezone.utc)
        if window.closes_at <= now:
            await warn(interaction, "Closing time must be in the future.")
            return
        await interaction.response.defer(ephemeral=True)
        target = await self.service.set_one_off_timing(
            target,
            window,
            reset_on_open=reset_on_open,
            now=now,
        )
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

        changes: dict[str, object] = {"schedule_enabled": int(enabled)}
        normalized_open = parse_day_rule(open_day) if open_day is not None else None
        normalized_close = parse_day_rule(close_day) if close_day is not None else None
        normalized_open_time = normalize_clock(open_time) if open_time is not None else None
        normalized_close_time = normalize_clock(close_time) if close_time is not None else None
        canonical_tz = canonical_timezone_name(timezone) if timezone is not None else None
        fixed_timezone = (
            fixed_utc_offset_name(canonical_tz)
            if canonical_tz is not None
            else None
        )
        if open_day is not None and normalized_open is None:
            unsupported_day = _unsupported_monthly_day(open_day)
            if unsupported_day is not None:
                await warn(
                    interaction,
                    f"Day {unsupported_day} isn't available every month. Use `last`, "
                    "`last-1`, or `last-2` for month-end timing.",
                )
                return
            await warn(
                interaction,
                "Enter the opening day as `1`–`28`, `last`, `last-1`, or `last-2`.",
            )
            return
        if close_day is not None and normalized_close is None:
            unsupported_day = _unsupported_monthly_day(close_day)
            if unsupported_day is not None:
                await warn(
                    interaction,
                    f"Day {unsupported_day} isn't available every month. Use `last`, "
                    "`last-1`, or `last-2` for month-end timing.",
                )
                return
            await warn(
                interaction,
                "Enter the closing day as `1`–`28`, `last`, `last-1`, or `last-2`.",
            )
            return
        if open_time is not None and normalized_open_time is None:
            await warn(interaction, "Enter the opening time in 24-hour `HH:mm` format.")
            return
        if close_time is not None and normalized_close_time is None:
            await warn(interaction, "Enter the closing time in 24-hour `HH:mm` format.")
            return
        if timezone is not None and canonical_tz is None:
            await warn(interaction, "Choose a timezone from the list.")
            return

        if normalized_open is not None:
            changes["open_day"] = normalized_open
        if normalized_close is not None:
            changes["close_day"] = normalized_close
        if normalized_open_time is not None:
            changes["open_time"] = normalized_open_time
        if normalized_close_time is not None:
            changes["close_time"] = normalized_close_time
        if fixed_timezone is not None:
            changes["schedule_utc_offset"] = fixed_timezone
        if reset_on_open is not None:
            changes["reset_on_open"] = int(reset_on_open)

        supplied_settings = len(changes) > 1
        if not enabled:
            await interaction.response.defer(ephemeral=True)
            target = await self.service.disable_schedule(target, changes)
            if supplied_settings:
                message = (
                    f"Saved the schedule for **{target.name}**. Automatic scheduling is off."
                )
            else:
                message = f"Disabled automatic scheduling for **{target.name}**."
            if target.status == "open":
                message += " The roster remains open."
            await interaction.followup.send(message, ephemeral=True)
            return

        effective_open = normalized_open or parse_day_rule(target.open_day or "")
        effective_close = normalized_close or parse_day_rule(target.close_day or "")
        effective_open_time = normalized_open_time or normalize_clock(target.open_time or "")
        effective_close_time = normalized_close_time or normalize_clock(target.close_time or "")
        effective_tz = fixed_timezone or canonical_timezone_name(target.schedule_utc_offset or "")
        if not all(
            (
                effective_open,
                effective_open_time,
                effective_close,
                effective_close_time,
                effective_tz,
            )
        ):
            await warn(
                interaction,
                "Enter the opening day and time, closing day and time, and timezone.",
            )
            return
        if target.one_off_open_ts is not None:
            await warn(
                interaction,
                "Clear the one-off timing before enabling automatic scheduling.",
            )
            return
        await interaction.response.defer(ephemeral=True)
        now = datetime.now(dt_timezone.utc)
        target = await self.service.configure_schedule(
            target,
            timezone_name=str(effective_tz),
            open_day=str(effective_open),
            open_time=str(effective_open_time),
            close_day=str(effective_close),
            close_time=str(effective_close_time),
            reset_on_open=(
                target.reset_on_open if reset_on_open is None else reset_on_open
            ),
            now=now,
        )

        message = f"Scheduled **{target.name}**."
        display_window = due_window(target, now)
        if not (
            target.status == "open"
            and display_window is not None
            and display_window.opens_at <= now < display_window.closes_at
        ):
            display_window = next_window(target, now)
            window_label = "Next window"
        else:
            window_label = "Current window"
        if display_window is not None:
            message += (
                f"\n{window_label}: {discord.utils.format_dt(display_window.opens_at)} to "
                f"{discord.utils.format_dt(display_window.closes_at)}."
            )
        await interaction.followup.send(
            message,
            ephemeral=True,
        )

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
        async with self._lock(target.id):
            current = await self.service.get(target.id)
            if current is None:
                await interaction.edit_original_response(
                    content="That roster no longer exists.",
                )
                return
            target = await self.service.open(current)
            await self.posts.post_interaction_response(target, interaction)

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
        clean_name = self._clean_roster_name(name)
        if clean_name is None:
            await warn(interaction, "Enter a roster name between 1 and 100 characters.")
            return
        try:
            clone = await self.service.clone(
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
                await self.service.delete(roster)
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
