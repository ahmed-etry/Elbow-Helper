"""Google Sheets publishing for roster signups."""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone as dt_timezone
import re

from discord.ext import commands

from elbow_helper.infrastructure.exports import ExportColumn
from elbow_helper.infrastructure.exports import ExportSheet
from elbow_helper.infrastructure.exports import GoogleSheetsPublisher

from ..repository import RosterRepository
from ..models import Roster
from .profiles import RosterProfileService


class RosterSheetPublisher:
    """Build and publish the current signup sheet for a roster."""

    def __init__(
        self,
        bot: commands.Bot,
        repository: RosterRepository,
        profiles: RosterProfileService,
        google_publisher: GoogleSheetsPublisher,
    ):
        self._bot = bot
        self._repository = repository
        self._profiles = profiles
        self._google_publisher = google_publisher

    async def export(self, roster: Roster) -> tuple[str | None, str | None]:
        members = await asyncio.to_thread(
            self._repository.list_members,
            roster.id,
            roster.active_cycle_id,
        )
        if not members:
            return None, f"No accounts are signed up to **{roster.name}**."
        members = await self._profiles.refresh(roster, members)
        guild = self._bot.get_guild(roster.guild_id)
        rows = []
        for member in members:
            discord_name = str(member.discord_user_id)
            if guild and (
                discord_member := guild.get_member(member.discord_user_id)
            ):
                discord_name = discord_member.display_name
            rows.append(
                (
                    discord_name,
                    member.player_name,
                    member.player_tag,
                    member.townhall or "-",
                    member.hero_sum or "-",
                    member.clan_code or "-",
                    datetime.fromtimestamp(
                        member.signed_up_ts,
                        dt_timezone.utc,
                    ).strftime("%Y-%m-%d %H:%M UTC"),
                )
            )
        sheet = ExportSheet(
            title="Roster",
            columns=(
                ExportColumn("Discord Member", 170),
                ExportColumn("Account", 170),
                ExportColumn("Player Tag", 120),
                ExportColumn("TH", 55, "right"),
                ExportColumn("Combined Hero Level", 135, "right"),
                ExportColumn("Current Clan", 100),
                ExportColumn("Signed Up", 155),
            ),
            rows=tuple(rows),
            tab_color="3B5B92",
        )
        link, warning = await self._google_publisher.upsert_spreadsheet(
            sheets=[sheet],
            sheet_title=f"{roster.name} [Roster]",
            spreadsheet_id=roster.google_sheet_id,
            cleanup_name_contains="[Roster]",
            retention_days=0,
        )
        if link:
            match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", link)
            if match and match.group(1) != roster.google_sheet_id:
                await asyncio.to_thread(
                    self._repository.update_roster,
                    roster.id,
                    google_sheet_id=match.group(1),
                )
        return link, warning
