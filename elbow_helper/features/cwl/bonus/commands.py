"""Discord command adapters for CWL bonus reports."""

from __future__ import annotations

import re
import copy

import discord
from discord import app_commands

from elbow_helper.discord.interactions import deny
from elbow_helper.configuration.roles import CWL_HELPERS
from elbow_helper.configuration.roles import LEAD_PLUS

from ..config import BONUS_CLAN_CHOICES
from ..config import CWL_CLAN_CODES
from .service import BonusReport
from .service import BonusReportError
from .settings import BonusExportView
from .settings import BonusSettingsButton
from .config import BonusConfigValidationError


def _error_preview(errors: list[str]) -> str:
    preview = "\n".join(f"- {error}" for error in errors[:12])
    if len(errors) > 12:
        preview += f"\n...and {len(errors) - 12} more"
    return preview


class CwlBonusMixin:
    def bonus_scoring_snapshot(self, clan_code: str):
        """Return one scoring setup and revision for panel and agent edits."""
        config, errors = self.bonus_config.load()
        if config is None:
            raise BonusConfigValidationError(errors)
        payload = (config.get("clans") or {}).get(clan_code)
        if not isinstance(payload, dict):
            raise BonusConfigValidationError([
                f"CWL bonus scoring settings for {clan_code} aren't available. Check that clan's settings and try again."
            ])
        meta = (config.get("clan_meta") or {}).get(clan_code) or {}
        return copy.deepcopy(payload), dict(meta), self.bonus_config.revision(config)

    def save_bonus_scoring(self, clan_code: str, payload, actor, *,
                           expected_revision: int, summary: str):
        """Save a complete scoring setup for panel and agent changes."""
        return self.bonus_config.save_clan(
            clan_code, payload, actor, expected_revision=expected_revision,
            summary=summary,
        )

    def bonus_scoring_issues(self, clan_code: str, payload) -> list[str]:
        """Validate a proposed complete setup before showing its preview."""
        return self.bonus_config._validate_bonus_clan_config(clan_code, payload)

    def copy_bonus_scoring(self, source: str, target: str, actor, *,
                           expected_revision: int):
        return self.bonus_config.copy_clan(
            source, target, actor, expected_revision=expected_revision,
        )

    async def cwl_bonus_season_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        clan_value = getattr(
            getattr(interaction, "namespace", None),
            "clan",
            None,
        )
        if isinstance(clan_value, app_commands.Choice):
            clan_value = clan_value.value
        selected_clans: list[str] | None = None
        if clan_value == "ALL":
            selected_clans = list(CWL_CLAN_CODES)
        elif clan_value in CWL_CLAN_CODES:
            selected_clans = [str(clan_value)]
        seasons = await self.bonus_reports.available_seasons(selected_clans)
        needle = str(current or "").strip().lower()
        return [
            app_commands.Choice(name=season, value=season)
            for season in seasons
            if needle in season.lower()
        ][:25]

    @app_commands.choices(clan=BONUS_CLAN_CHOICES)
    @app_commands.autocomplete(season=cwl_bonus_season_autocomplete)
    @app_commands.describe(
        clan="Clan to include, or ALL for the entire family.",
        season=(
            "Completed CWL season. Uses the latest available season by "
            "default."
        ),
    )
    async def cwl_bonus(
        self,
        interaction: discord.Interaction,
        clan: app_commands.Choice[str],
        season: str | None = None,
    ) -> None:
        if not self._has_any_role(
            interaction,
            LEAD_PLUS | CWL_HELPERS,
        ):
            await deny(interaction)
            return
        await interaction.response.defer(thinking=True)
        try:
            report = await self.bonus_reports.create(
                clan.value,
                season,
            )
        except BonusReportError as error:
            await interaction.followup.send(
                self.bonus_report_error_message(error)
            )
            return
        await self._send_bonus_report(interaction, report)

    @staticmethod
    def bonus_report_error_message(error: BonusReportError) -> str:
        if error.kind == "config":
            return (
                "The CWL bonus settings contain errors, so this report "
                "can't be created. Check the CWL bonus settings and try "
                "again.\n"
                f"{_error_preview(error.details)}"
            )
        if error.kind == "no_seasons":
            return (
                "No completed CWL season is available for that clan "
                "selection yet."
            )
        if error.kind == "invalid_season":
            preview = ", ".join(error.available_seasons[:8])
            return (
                "No completed CWL results are available for "
                f"`{error.requested_season}` with that clan selection.\n"
                f"Available seasons: {preview}"
            )
        if error.kind == "scoring":
            return (
                "The CWL bonus report couldn't be created because some "
                "clan scoring settings are missing or invalid. Check those "
                "settings and try again.\n"
                f"{_error_preview(error.details)}"
            )
        return "Couldn't generate the spreadsheet. Try again in a moment."

    async def _send_bonus_report(
        self,
        interaction: discord.Interaction,
        report: BonusReport,
    ) -> None:
        response_lines = bonus_report_lines(report)

        view = BonusExportView()
        google_download_link = None
        if report.google_link:
            match = re.search(
                r"/spreadsheets/d/([a-zA-Z0-9-_]+)",
                report.google_link,
            )
            if match:
                google_download_link = (
                    "https://docs.google.com/spreadsheets/d/"
                    f"{match.group(1)}/export?format=xlsx"
                )
            view.add_item(
                discord.ui.Button(
                    label="Google Sheet",
                    style=discord.ButtonStyle.link,
                    url=report.google_link,
                )
            )
        if google_download_link:
            view.add_item(
                discord.ui.Button(
                    label="Download",
                    style=discord.ButtonStyle.link,
                    url=google_download_link,
                )
            )
        view.add_item(
            BonusSettingsButton(
                self,
                list(report.selected_clans),
            )
        )

        delivered = False
        try:
            title = (
                "**CWL Bonus Estimate "
                f"({report.scope_label}, {report.season})**"
            )
            if report.google_link and view.children:
                message_lines = [title]
                if report.warnings:
                    preview = " | ".join(report.warnings[:3])
                    suffix = (
                        f" (+{len(report.warnings) - 3} more)"
                        if len(report.warnings) > 3
                        else ""
                    )
                    message_lines.append(f"Warnings: {preview}{suffix}")
                message = await interaction.followup.send(
                    "\n".join(message_lines),
                    view=view,
                    wait=True,
                )
                delivered = True
                view.bind_message(message)
                return

            attachment = discord.File(
                str(report.workbook_path),
                filename=report.workbook_name,
            )
            try:
                message = await interaction.followup.send(
                    "\n".join(response_lines),
                    wait=True,
                    file=attachment,
                )
            finally:
                attachment.close()
            delivered = bool(message.attachments)
            if message.attachments:
                view.add_item(
                    discord.ui.Button(
                        label="Download",
                        style=discord.ButtonStyle.link,
                        url=message.attachments[0].url,
                    )
                )
            if view.children:
                await message.edit(content="\n".join(response_lines), view=view)
                view.bind_message(message)
        finally:
            if delivered:
                await self.bonus_reports.discard(report)


def bonus_report_lines(report: BonusReport) -> list[str]:
    response_lines = [
        (
            "CWL bonus estimate ready for "
            f"`{report.scope_label}` (`{report.season}`)."
        ),
        f"Eligible players: {report.eligible_count}",
        f"Ineligible players: {report.ineligible_count}",
        f"Scored attacks: {report.attack_count}",
    ]
    if report.warnings:
        preview = " | ".join(report.warnings[:5])
        suffix = (
            f" (+{len(report.warnings) - 5} more)"
            if len(report.warnings) > 5
            else ""
        )
        response_lines.append(f"Warnings: {preview}{suffix}")
    if report.google_warning and not report.google_link:
        warning = report.google_warning.rstrip(".")
        response_lines.append(
            f"{warning}. The Excel file is ready to download."
        )

    return response_lines
