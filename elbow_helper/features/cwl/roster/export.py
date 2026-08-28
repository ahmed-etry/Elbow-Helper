"""Workbook assembly and delivery helpers for CWL roster planning."""

from __future__ import annotations

import asyncio
import os
import re
import zipfile
from datetime import datetime
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Any
from typing import Optional
from typing import Sequence
from xml.etree import ElementTree

import discord

from elbow_helper.features.records.domain.types import category_label
from elbow_helper.features.records.domain.types import incident_type_label
from elbow_helper.infrastructure.exports import ExportColumn
from elbow_helper.infrastructure.exports import ExportSheet
from elbow_helper.infrastructure.exports import xlsx_column_name
from elbow_helper.configuration.clans import CLAN_ORDER

from ..helpers import format_cwl_season_label
from .models import AssSeasonMetric
from .models import MegaAssMetric

UTC = dt_timezone.utc
ROSTER_TAB_COLORS = {
    "Roster Planner": "3B5B92",
    "Season History": "3E83AE",
    "Leadership Records": "64748B",
    "Guide": "64748B",
}


def _format_date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "-"


def _rounded(value: float | None) -> float | str:
    return round(float(value), 2) if value is not None else "-"


class CwlRosterExportMixin:
    @staticmethod
    def _build_roster_workbook(
        *,
        candidates: Sequence[dict[str, Any]],
        signed_tags: set[str],
        season_metrics: Sequence[AssSeasonMetric],
        records: Sequence[dict[str, Any]],
        links_by_user: dict[int, list[dict[str, Any]]],
        seasons: Sequence[dict[str, Any]],
        profiles: dict[str, Any],
        latest_leagues: dict[str, str],
        clan_choices: Sequence[str],
    ) -> list[ExportSheet]:
        planner_columns = (
            ExportColumn("Discord Member", 150),
            ExportColumn("Account", 150),
            ExportColumn("Player Tag", 105),
            ExportColumn("TH", 55, "right"),
            ExportColumn("Current Clan", 85),
            ExportColumn("Latest CWL Clan", 95),
            ExportColumn("Latest Season", 105),
            ExportColumn("ASS", 75, "right"),
            ExportColumn("Rank", 70, "center"),
            ExportColumn("Average Defense Position", 105, "right"),
            ExportColumn("Attacks", 75, "center"),
            ExportColumn("Same-Clan ASS", 95, "right"),
            ExportColumn("Same-Clan Rank", 95, "center"),
            ExportColumn("Same-Clan Sample", 125),
            ExportColumn("CWL Records", 90, "right"),
            ExportColumn("Assigned Clan", 110),
        )
        planner_rows: list[tuple[Any, ...]] = []
        for row in candidates:
            latest: AssSeasonMetric | None = row.get("latest")
            mega: MegaAssMetric | None = row.get("mega")
            planner_rows.append(
                (
                    str(row.get("discord_member") or "-"),
                    str(row.get("account_name") or "-"),
                    str(row.get("player_tag") or "-"),
                    int(row.get("townhall") or 0) or "-",
                    str(row.get("current_clan") or "-"),
                    latest.clan_code if latest else "-",
                    format_cwl_season_label(latest.season) if latest else "-",
                    _rounded(latest.score if latest else None),
                    latest.rank_label if latest else "-",
                    _rounded(
                        latest.average_defensive_position
                        if latest
                        else None
                    ),
                    latest.attacks_label if latest else "-",
                    _rounded(mega.score if mega else None),
                    mega.rank_label if mega else "-",
                    (
                        f"{len(mega.seasons)} "
                        f"{'season' if len(mega.seasons) == 1 else 'seasons'} / "
                        f"{mega.total_attacks} "
                        f"{'attack' if mega.total_attacks == 1 else 'attacks'}"
                        if mega
                        else "-"
                    ),
                    int(row.get("cwl_records") or 0),
                    "",
                )
            )

        clan_order = {code: index for index, code in enumerate(CLAN_ORDER)}
        history_columns = (
            ExportColumn("Season", 115),
            ExportColumn("CWL Clan", 75),
            ExportColumn("League", 130),
            ExportColumn("Account", 150),
            ExportColumn("TH", 55, "right"),
            ExportColumn("Profile", 210),
            ExportColumn("ASS", 75, "right"),
            ExportColumn("Rank", 70, "center"),
            ExportColumn("Attacks", 75, "center"),
            ExportColumn("Missed Attacks", 95, "right"),
            ExportColumn("Stars", 60, "right"),
            ExportColumn("Average Destruction", 105, "right"),
            ExportColumn("Average Target Position", 110, "right"),
            ExportColumn("Average Defense Position", 105, "right"),
            ExportColumn("Missed Stars", 85, "right"),
            ExportColumn("Missed Adjustment", 105, "right"),
            ExportColumn("Difficulty Adjustment", 115, "right"),
        )
        history_rows: list[tuple[Any, ...]] = []
        for row in sorted(
            (metric for metric in season_metrics if metric.player_tag in signed_tags),
            key=lambda item: (
                -item.season_order,
                clan_order.get(item.clan_code, len(clan_order)),
                item.rank if item.rank is not None else 10_000,
                item.player_name.casefold(),
            ),
        ):
            history_rows.append(
                (
                    format_cwl_season_label(row.season),
                    row.clan_code,
                    row.league or "Unknown",
                    row.player_name,
                    row.townhall,
                    row.profile.label,
                    _rounded(row.score),
                    row.rank_label,
                    row.attacks_label,
                    max(0, row.attacks_expected - row.attacks),
                    row.stars,
                    _rounded(row.average_destruction),
                    _rounded(row.average_target_position),
                    _rounded(row.average_defensive_position),
                    _rounded(row.missed_stars),
                    _rounded(row.missed_adjustment),
                    _rounded(row.difficulty_adjustment),
                )
            )

        context_columns = (
            ExportColumn("Recorded Date", 105),
            ExportColumn("Discord Member", 150),
            ExportColumn("Linked Accounts", 250),
            ExportColumn("Category", 105),
            ExportColumn("Type", 190),
            ExportColumn("Details", 420),
            ExportColumn("Recorded By", 150),
            ExportColumn("Last Updated", 105),
        )
        context_rows: list[tuple[Any, ...]] = []
        for record in records:
            member_id = int(record.get("member_id") or 0)
            account_labels: list[str] = []
            for link in links_by_user.get(member_id, []):
                name = str(
                    link.get("player_name_last_seen")
                    or link.get("player_tag")
                    or "Unknown"
                )
                account_labels.append(name)
            context_rows.append(
                (
                    _format_date(record.get("created_ts")),
                    str(record.get("member_display") or member_id),
                    "\n".join(account_labels) if account_labels else "-",
                    category_label(str(record.get("category_key") or "")),
                    incident_type_label(
                        str(record.get("incident_type_key") or "")
                    ),
                    str(record.get("note") or ""),
                    str(record.get("recorder_display") or "-"),
                    _format_date(record.get("updated_ts")),
                )
            )
        if not context_rows:
            context_rows.append(("-", "No active records", "-", "-", "-", "-", "-", "-"))

        profile_lines = []
        for clan_code in CLAN_ORDER:
            profile = profiles.get(clan_code)
            if profile is None:
                continue
            profile_lines.append(
                (
                    f"{clan_code} profile",
                    (
                        f"{profile.label}, based on the clan's most recent completed "
                        f"CWL league ({latest_leagues.get(clan_code, 'Unknown')}). "
                        "The clan's current league may be different."
                    ),
                    f"{clan_code}: {profile.difficulty_weight:g} difficulty points per position.",
                )
            )
        period_text = ", ".join(
            format_cwl_season_label(str(row.get("key") or ""))
            for row in seasons
        )
        guide_rows = [
            (
                "Roster Planner",
                "Current CWL signups and every Clash account linked to them. "
                "Leadership can assign each account in the Assigned Clan column.",
                "Choose a clan from the dropdown in Assigned Clan.",
            ),
            (
                "ASS",
                "A standardized measure of how an account's attacks contributed "
                "relative to teammates in the same clan and season. It is not "
                "a universal skill score.",
                "A rank of 3/16 means third among 16 scored accounts in that "
                "comparison group.",
            ),
            (
                "Same-Clan ASS",
                "The average of a player's seasonal ASS scores in the latest "
                "CWL clan shown on the planner. If an account moved clans, "
                "the other seasons remain visible in Season History.",
                "Three same-clan scores of 18, 20, and 22 average to 20.",
            ),
            (
                "Sample",
                "The number of scored seasons and attacks behind Same-Clan ASS. "
                "Smaller samples are shown rather than treated as equally "
                "reliable.",
                "2 seasons / 9 attacks.",
            ),
            (
                "Rate adjustment",
                "Seasonal stars and missed stars are projected to seven attacks. "
                "Accounts with no attacks remain unranked because a rate cannot "
                "be calculated.",
                "Six stars from two attacks projects to 21 stars over seven "
                "attacks.",
            ),
            (
                "Average defensive position",
                "The account's average map position in the wars where it attacked.",
                "Positions 4, 5, and 6 average to 5.",
            ),
            (
                "High League Standard",
                "Used when the clan's most recent completed CWL league is Master I "
                "or Champion. Difficulty is worth 0.4 per position; missed-star "
                "pressure begins above one projected miss.",
                "Two projected missed stars apply a -2 adjustment.",
            ),
            (
                "Lower League Standard",
                "Used below Master I. Difficulty is worth 1 point per position; "
                "projected missed stars are not adjusted until they exceed two.",
                "Three projected missed stars apply a -3 adjustment.",
            ),
            (
                "Season History",
                "Shows signed-up accounts by CWL season, clan, league, and "
                "scoring profile. Rankings still include everyone who was on that "
                "clan's CWL roster for the season, even though non-signups are "
                "hidden here.",
                "An account with one season in BE4 and two in BES has three "
                "separate rows.",
            ),
            (
                "Included history",
                "The completed CWL seasons included in this workbook.",
                period_text or "No completed seasons.",
            ),
            *profile_lines,
        ]
        guide_columns = (
            ExportColumn("Metric", 180),
            ExportColumn("Explanation", 520),
            ExportColumn("Example", 360),
        )

        return [
            ExportSheet(
                title="Roster Planner",
                columns=planner_columns,
                rows=tuple(planner_rows),
                tab_color=ROSTER_TAB_COLORS["Roster Planner"],
                dropdowns=((len(planner_columns) - 1, tuple(clan_choices)),),
            ),
            ExportSheet(
                title="Season History",
                columns=history_columns,
                rows=tuple(history_rows),
                tab_color=ROSTER_TAB_COLORS["Season History"],
            ),
            ExportSheet(
                title="Leadership Records",
                columns=context_columns,
                rows=tuple(context_rows),
                tab_color=ROSTER_TAB_COLORS["Leadership Records"],
            ),
            ExportSheet(
                title="Guide",
                columns=guide_columns,
                rows=tuple(guide_rows),
                tab_color=ROSTER_TAB_COLORS["Guide"],
            ),
        ]

    @staticmethod
    def _roster_rows_for_xlsx(
        sheets: Sequence[ExportSheet],
    ) -> list[tuple[str, list[list[Any]]]]:
        return [
            (
                sheet.title,
                [
                    [column.name for column in sheet.columns],
                    *[list(row) for row in sheet.rows],
                ],
            )
            for sheet in sheets
        ]

    @staticmethod
    def _apply_roster_xlsx_features(
        workbook_path: Path,
        sheets: Sequence[ExportSheet],
    ) -> None:
        namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        ElementTree.register_namespace("", namespace)
        temporary_path = workbook_path.with_name(f"{workbook_path.name}.roster.tmp")
        try:
            with zipfile.ZipFile(workbook_path, "r") as source:
                with zipfile.ZipFile(temporary_path, "w") as target:
                    for item in source.infolist():
                        data = source.read(item.filename)
                        match = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", item.filename)
                        if match:
                            index = int(match.group(1)) - 1
                            if 0 <= index < len(sheets):
                                sheet = sheets[index]
                                root = ElementTree.fromstring(data)
                                sheet_properties = root.find(f"{{{namespace}}}sheetPr")
                                if sheet_properties is None:
                                    sheet_properties = ElementTree.Element(
                                        f"{{{namespace}}}sheetPr"
                                    )
                                    root.insert(0, sheet_properties)
                                tab_color = sheet_properties.find(
                                    f"{{{namespace}}}tabColor"
                                )
                                if tab_color is None:
                                    tab_color = ElementTree.SubElement(
                                        sheet_properties,
                                        f"{{{namespace}}}tabColor",
                                    )
                                tab_color.set("rgb", f"FF{sheet.tab_color}")

                                if sheet.rows and sheet.dropdowns:
                                    validations = ElementTree.Element(
                                        f"{{{namespace}}}dataValidations",
                                        {"count": str(len(sheet.dropdowns))},
                                    )
                                    for column_index, choices in sheet.dropdowns:
                                        column_name = xlsx_column_name(
                                            column_index + 1
                                        )
                                        validation = ElementTree.SubElement(
                                            validations,
                                            f"{{{namespace}}}dataValidation",
                                            {
                                                "type": "list",
                                                "allowBlank": "1",
                                                "showErrorMessage": "1",
                                                "errorStyle": "stop",
                                                "errorTitle": "Invalid clan",
                                                "error": "Choose a clan from the available options.",
                                                "sqref": (
                                                    f"{column_name}2:"
                                                    f"{column_name}{len(sheet.rows) + 1}"
                                                ),
                                            },
                                        )
                                        formula = ElementTree.SubElement(
                                            validation,
                                            f"{{{namespace}}}formula1",
                                        )
                                        formula.text = f'"{",".join(choices)}"'
                                    root.append(validations)
                                data = ElementTree.tostring(
                                    root,
                                    encoding="utf-8",
                                    xml_declaration=True,
                                )
                        target.writestr(item, data)
            os.replace(temporary_path, workbook_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _write_roster_xlsx(
        self,
        workbook_path: Path,
        sheets: Sequence[ExportSheet],
    ) -> None:
        self.workbook_writer.write(
            workbook_path,
            self._roster_rows_for_xlsx(sheets),
        )
        self._apply_roster_xlsx_features(workbook_path, sheets)

    async def _send_roster_workbook(
        self,
        *,
        interaction: discord.Interaction,
        sheets: Sequence[ExportSheet],
        history_label: str,
        signed_member_count: int,
        signed_account_count: int,
    ) -> None:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        workbook_name = f"cwl_roster_{timestamp}.xlsx"
        workbook_path = self.cwl_exports.path_for(workbook_name)
        try:
            await asyncio.to_thread(
                self._write_roster_xlsx,
                workbook_path,
                sheets,
            )
        except (OSError, TypeError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
            await interaction.followup.send(
                "Couldn't generate the roster planner. Try again in a moment.",
                ephemeral=True,
            )
            return

        google_link: Optional[str] = None
        google_warning: Optional[str] = None
        guild_name = str(
            getattr(getattr(interaction, "guild", None), "name", "") or "CWL"
        )
        sheet_title = f"{guild_name} [CWL Roster Planner] {timestamp}"
        google_link, google_warning = (
            await self.google_publisher.upsert_spreadsheet(
                sheets=sheets,
                sheet_title=sheet_title,
                cleanup_name_contains="[CWL Roster Planner]",
                retention_days=self.cwl_exports.retention_days,
            )
        )

        _, cleanup_warning = await asyncio.to_thread(
            self.cwl_exports.cleanup,
            "cwl_roster_*.xlsx",
        )
        if cleanup_warning:
            LOGGER.warning("Local cleanup warning: %s", cleanup_warning)
        lines = [
            f"**CWL Roster Planner ({history_label})**",
            f"**{signed_member_count}** members • "
            f"**{signed_account_count}** Clash accounts signed up",
        ]

        view = discord.ui.View(timeout=None)
        if google_link:
            view.add_item(
                discord.ui.Button(
                    label="Google Sheet",
                    style=discord.ButtonStyle.link,
                    url=google_link,
                )
            )
            match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", google_link)
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
            await interaction.followup.send(
                "\n".join(lines),
                view=view,
                ephemeral=True,
            )
            return

        if google_warning:
            lines.append(google_warning)
        message = await interaction.followup.send(
            "\n".join(lines),
            wait=True,
            file=discord.File(str(workbook_path), filename=workbook_name),
            ephemeral=True,
        )
        if message.attachments:
            view.add_item(
                discord.ui.Button(
                    label="Download",
                    style=discord.ButtonStyle.link,
                    url=message.attachments[0].url,
                )
            )
            await message.edit(content="\n".join(lines), view=view)
