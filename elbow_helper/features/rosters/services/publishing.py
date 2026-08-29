"""Spreadsheet exports for roster signups."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as dt_timezone
import logging
from pathlib import Path
import re
import unicodedata

from discord.ext import commands

from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import LocalExportStore
from elbow_helper.infrastructure.exports import WorkbookWriter

from ..models import Roster
from ..repository import RosterRepository
from .profiles import RosterProfileService


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RosterExport:
    workbook_path: Path
    workbook_name: str
    google_link: str | None
    google_warning: str | None


def _filename_segment(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return (
        re.sub(r"[^a-z0-9]+", "_", ascii_value.casefold()).strip("_")
        or "roster"
    )


class RosterSheetPublisher:
    """Build and publish a snapshot of the current roster signups."""

    def __init__(
        self,
        bot: commands.Bot,
        repository: RosterRepository,
        profiles: RosterProfileService,
        google_publisher: GoogleSheetsPublisher,
        workbook_writer: WorkbookWriter,
        local_exports: LocalExportStore,
    ):
        self._bot = bot
        self._repository = repository
        self._profiles = profiles
        self._google_publisher = google_publisher
        self._workbook_writer = workbook_writer
        self._local_exports = local_exports

    async def export(
        self,
        roster: Roster,
    ) -> tuple[RosterExport | None, str | None]:
        deleted, cleanup_warning = await asyncio.to_thread(
            self._local_exports.cleanup,
            "*.xlsx",
        )
        if deleted:
            LOGGER.info("Deleted %s abandoned local export files", deleted)
        if cleanup_warning:
            LOGGER.warning("Local cleanup warning: %s", cleanup_warning)

        members = await asyncio.to_thread(
            self._repository.list_members,
            roster.id,
            roster.active_cycle_id,
        )
        if not members:
            return None, f"No accounts are signed up to **{roster.name}**."
        members = await self._profiles.refresh(roster, members)
        guild = self._bot.get_guild(roster.guild_id)
        rows: list[list[object]] = [[
            "Discord Member",
            "Account",
            "Player Tag",
            "TH",
            "Combined Hero Level",
            "Current Clan",
            "Signed Up",
        ]]
        for member in members:
            discord_name = str(member.discord_user_id)
            if guild and (
                discord_member := guild.get_member(member.discord_user_id)
            ):
                discord_name = discord_member.display_name
            rows.append([
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
            ])

        timestamp = datetime.now(dt_timezone.utc).strftime("%Y%m%d-%H%M%S")
        workbook_name = (
            f"roster_{_filename_segment(roster.name)}_{timestamp}.xlsx"
        )
        workbook_path = self._local_exports.temporary_path("roster")
        await asyncio.to_thread(
            self._workbook_writer.write,
            workbook_path,
            [("Roster", rows)],
        )
        google_link, google_warning = await self._google_publisher.upload_workbook(
            workbook_path,
            f"{roster.name} [Roster] {timestamp}",
        )
        return RosterExport(
            workbook_path=workbook_path,
            workbook_name=workbook_name,
            google_link=google_link,
            google_warning=google_warning,
        ), None

    async def discard(self, report: RosterExport) -> None:
        warning = await asyncio.to_thread(
            self._local_exports.delete,
            report.workbook_path,
        )
        if warning:
            LOGGER.warning("Local cleanup warning: %s", warning)
