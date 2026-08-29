"""CWL bonus report workflow and export publishing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as dt_timezone
import logging
from pathlib import Path
from typing import Any

from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import LocalExportStore

from ..config import CWL_CLAN_CODES
from .analysis import BonusAnalysisService
from .config import BonusConfigRepository
from .export import BonusWorkbookWriter


LOGGER = logging.getLogger(__name__)


class BonusReportError(RuntimeError):
    """A classified report failure that a Discord adapter can explain."""

    def __init__(
        self,
        kind: str,
        details: list[str] | None = None,
        *,
        available_seasons: list[str] | None = None,
        requested_season: str | None = None,
    ):
        self.kind = kind
        self.details = details or []
        self.available_seasons = available_seasons or []
        self.requested_season = requested_season
        super().__init__(kind)


@dataclass(frozen=True, slots=True)
class BonusReport:
    scope_label: str
    season: str
    selected_clans: tuple[str, ...]
    workbook_path: Path
    workbook_name: str
    google_link: str | None
    google_warning: str | None
    eligible_count: int
    ineligible_count: int
    attack_count: int
    warnings: tuple[str, ...]


class BonusReportService:
    """Build, retain, and publish CWL bonus recommendation workbooks."""

    def __init__(
        self,
        analysis: BonusAnalysisService,
        config: BonusConfigRepository,
        workbook_writer: BonusWorkbookWriter,
        google_publisher: GoogleSheetsPublisher,
        exports: LocalExportStore,
    ):
        self._analysis = analysis
        self._config = config
        self._workbook_writer = workbook_writer
        self._google_publisher = google_publisher
        self._exports = exports

    async def available_seasons(
        self,
        selected_clans: list[str] | None,
    ) -> list[str]:
        return await asyncio.to_thread(
            self._analysis.seasons,
            selected_clans,
        )

    async def create(
        self,
        scope: str,
        season: str | None = None,
    ) -> BonusReport:
        config, config_errors = await asyncio.to_thread(self._config.load)
        if config is None:
            raise BonusReportError("config", config_errors)

        selected_clans = (
            list(CWL_CLAN_CODES)
            if scope == "ALL"
            else [scope]
        )
        available_seasons = await self.available_seasons(selected_clans)
        if not available_seasons:
            raise BonusReportError("no_seasons")
        selected_season = str(season or "").strip() or available_seasons[0]
        if selected_season not in available_seasons:
            raise BonusReportError(
                "invalid_season",
                available_seasons=available_seasons,
                requested_season=selected_season,
            )

        all_summary: list[dict[str, Any]] = []
        all_ineligible: list[dict[str, Any]] = []
        all_raw: list[dict[str, Any]] = []
        all_warnings: list[str] = []
        all_errors: list[str] = []
        per_clan_summary: dict[str, list[dict[str, Any]]] = {}
        per_clan_ineligible: dict[str, list[dict[str, Any]]] = {}
        per_clan_raw: dict[str, list[dict[str, Any]]] = {}

        for clan_code in selected_clans:
            (
                summary_rows,
                ineligible_rows,
                raw_rows,
                warnings,
                errors,
            ) = await asyncio.to_thread(
                self._analysis.analyze_clan,
                clan_code,
                selected_season,
                config,
            )
            all_summary.extend(summary_rows)
            all_ineligible.extend(ineligible_rows)
            all_raw.extend(raw_rows)
            all_warnings.extend(warnings)
            all_errors.extend(errors)
            per_clan_summary[clan_code] = summary_rows
            per_clan_ineligible[clan_code] = ineligible_rows
            per_clan_raw[clan_code] = raw_rows

        if all_errors:
            raise BonusReportError("scoring", all_errors)

        all_summary.sort(
            key=lambda row: (row["clan"], int(row.get("rank") or 0))
        )
        all_ineligible.sort(
            key=lambda row: (
                row["clan"],
                str(row["player_name"]).lower(),
            )
        )
        all_raw.sort(
            key=lambda row: (
                row["clan"],
                int(row["round"]),
                str(row["player_name"]).lower(),
            )
        )
        workbook_sheets = self._build_sheets(
            selected_clans,
            all_summary,
            per_clan_summary,
            per_clan_ineligible,
            per_clan_raw,
        )

        timestamp = datetime.now(dt_timezone.utc).strftime("%Y%m%d-%H%M%S")
        season_slug = selected_season.replace("-", "")
        workbook_name = (
            f"cwl_bonus_{scope.lower()}_{season_slug}_{timestamp}.xlsx"
        )
        workbook_path = self._exports.temporary_path("cwl_bonus")
        try:
            await asyncio.to_thread(
                self._workbook_writer.write,
                workbook_path,
                workbook_sheets,
                workbook_clan_code=(
                    selected_clans[0]
                    if len(selected_clans) == 1
                    else None
                ),
            )
        except (OSError, TypeError, ValueError) as error:
            raise BonusReportError("workbook") from error

        deleted, cleanup_warning = await asyncio.to_thread(
            self._exports.cleanup,
            "*.xlsx",
        )
        if deleted:
            LOGGER.info("Deleted %s old local export files", deleted)
        if cleanup_warning:
            LOGGER.warning("Local cleanup warning: %s", cleanup_warning)

        sheet_title = (
            f"CWL Bonus Estimate {scope} {selected_season} {timestamp}"
        )
        google_link, google_warning = (
            await self._google_publisher.upload_workbook(
                workbook_path,
                sheet_title,
            )
        )
        return BonusReport(
            scope_label=scope,
            season=selected_season,
            selected_clans=tuple(selected_clans),
            workbook_path=workbook_path,
            workbook_name=workbook_name,
            google_link=google_link,
            google_warning=google_warning,
            eligible_count=len(all_summary),
            ineligible_count=len(all_ineligible),
            attack_count=len(all_raw),
            warnings=tuple(all_warnings),
        )

    async def discard(self, report: BonusReport) -> None:
        warning = await asyncio.to_thread(
            self._exports.delete,
            report.workbook_path,
        )
        if warning:
            LOGGER.warning("Local cleanup warning: %s", warning)

    def _build_sheets(
        self,
        selected_clans: list[str],
        all_summary: list[dict[str, Any]],
        per_clan_summary: dict[str, list[dict[str, Any]]],
        per_clan_ineligible: dict[str, list[dict[str, Any]]],
        per_clan_raw: dict[str, list[dict[str, Any]]],
    ) -> list[tuple[str, list[list[Any]]]]:
        summary_sheet: list[list[Any]] = []
        if len(selected_clans) > 1:
            summary_sheet.append([
                "Overall Rank",
                "Clan",
                "Player",
                "Attacks",
                "Average Final Delta per Attack",
                "Average Actual Score (AS)",
                "Average Expected Score (ES)",
            ])
            if all_summary:
                overall_rows = sorted(
                    all_summary,
                    key=lambda row: (
                        -float(row["avg_adjusted_delta"]),
                        -float(row["total_adjusted_delta"]),
                        -float(row["total_actual"]),
                        -int(row["attack_count"]),
                        str(row["player_name"]).lower(),
                    ),
                )
                for overall_rank, row in enumerate(
                    overall_rows,
                    start=1,
                ):
                    attacks = int(row["attack_count"])
                    average_actual = (
                        float(row["total_actual"]) / attacks
                        if attacks
                        else 0.0
                    )
                    average_expected = (
                        float(row["total_expected"]) / attacks
                        if attacks
                        else 0.0
                    )
                    summary_sheet.append([
                        overall_rank,
                        row["clan"],
                        row["player_name"],
                        attacks,
                        round(float(row["avg_adjusted_delta"]), 3),
                        round(average_actual, 3),
                        round(average_expected, 3),
                    ])
            else:
                summary_sheet.append(
                    ["-", "None", "-", "-", "-", "-", "-"]
                )

            summary_sheet.append([])
            summary_sheet.append(["Clan Overview"])
            summary_sheet.append([
                "Clan",
                "Eligible Players",
                "Ineligible Players",
                "Total Attacks",
                "Average Final Delta per Attack",
                "Average Actual Score (AS)",
                "Average Expected Score (ES)",
                "Top Player",
            ])
            for clan_code in selected_clans:
                clan_rows = per_clan_summary.get(clan_code, [])
                ineligible_count = len(
                    per_clan_ineligible.get(clan_code, [])
                )
                if clan_rows:
                    total_attacks = sum(
                        int(row.get("attack_count") or 0)
                        for row in clan_rows
                    )
                    total_final = sum(
                        float(row.get("total_adjusted_delta") or 0.0)
                        for row in clan_rows
                    )
                    total_actual = sum(
                        float(row.get("total_actual") or 0.0)
                        for row in clan_rows
                    )
                    total_expected = sum(
                        float(row.get("total_expected") or 0.0)
                        for row in clan_rows
                    )
                    average_final = (
                        total_final / total_attacks
                        if total_attacks
                        else 0.0
                    )
                    average_actual = (
                        total_actual / total_attacks
                        if total_attacks
                        else 0.0
                    )
                    average_expected = (
                        total_expected / total_attacks
                        if total_attacks
                        else 0.0
                    )
                    top_player = max(
                        clan_rows,
                        key=lambda row: (
                            float(
                                row.get("avg_adjusted_delta") or 0.0
                            ),
                            float(
                                row.get("total_adjusted_delta") or 0.0
                            ),
                        ),
                    ).get("player_name", "-")
                else:
                    total_attacks = 0
                    average_final = 0.0
                    average_actual = 0.0
                    average_expected = 0.0
                    top_player = "-"
                summary_sheet.append([
                    clan_code,
                    len(clan_rows),
                    ineligible_count,
                    total_attacks,
                    round(average_final, 3),
                    round(average_actual, 3),
                    round(average_expected, 3),
                    top_player,
                ])

        raw_sheet_header = [
            "Round",
            "Player",
            "Attacker Town Hall",
            "Defender Tag",
            "Defender Town Hall",
            "Stars",
            "Destruction %",
            "Actual Score (AS)",
            "Expected Score (ES)",
            "TH Difference (Defender − Attacker)",
            "Base Delta",
            "TH Adjustment (+up/-down)",
            "Final Delta",
            "Star Gain",
            "Flags (if any)",
        ]
        workbook_sheets: list[tuple[str, list[list[Any]]]] = [
            ("Guide", self._workbook_writer.guide_sheet())
        ]
        if len(selected_clans) > 1:
            workbook_sheets.append(("Summary", summary_sheet))
        for clan_code in selected_clans:
            clan_rows = per_clan_summary.get(clan_code, [])
            clan_sheet: list[list[Any]] = [[
                "Rank",
                "Player",
                "Attacks",
                "Actual Score (AS)",
                "Expected Score (ES)",
                "Base Delta (AS-ES)",
                "TH Adjustment",
                "Average Final Delta per Attack",
            ]]
            if clan_rows:
                for row in clan_rows:
                    clan_sheet.append([
                        row["rank"],
                        row["player_name"],
                        row["attack_count"],
                        round(float(row["total_actual"]), 3),
                        round(float(row["total_expected"]), 3),
                        round(float(row["total_base_delta"]), 3),
                        round(float(row["total_adjustment"]), 3),
                        round(float(row["avg_adjusted_delta"]), 3),
                    ])
            else:
                clan_sheet.append(
                    ["-", "None", "-", "-", "-", "-", "-", "-"]
                )
            clan_sheet.append([])
            clan_sheet.append(["Ineligible Players", "Missed Attacks"])
            ineligible_rows = per_clan_ineligible.get(clan_code, [])
            if ineligible_rows:
                for row in sorted(
                    ineligible_rows,
                    key=lambda item: str(
                        item["player_name"]
                    ).lower(),
                ):
                    clan_sheet.append([
                        row["player_name"],
                        row["missed_attacks"],
                    ])
            else:
                clan_sheet.append(["None", 0])
            summary_name = (
                "Summary"
                if len(selected_clans) == 1
                else f"{clan_code}_Summary"
            )
            workbook_sheets.append((summary_name, clan_sheet))

            raw_rows = sorted(
                per_clan_raw.get(clan_code, []),
                key=lambda item: (
                    int(item.get("round") or 0),
                    str(item.get("player_name") or "").lower(),
                    str(item.get("war_tag") or ""),
                ),
            )
            raw_sheet: list[list[Any]] = [raw_sheet_header]
            if raw_rows:
                for row in raw_rows:
                    raw_sheet.append([
                        row["round"],
                        row["player_name"],
                        row["attacker_th"],
                        row["defender_tag"],
                        row["defender_th"],
                        row["stars"],
                        round(float(row["destruction"]), 2),
                        round(float(row["actual_score"]), 3),
                        round(float(row["expected_score"]), 3),
                        row["th_gap"],
                        round(float(row["base_delta"]), 3),
                        round(float(row["delta_adjustment"]), 3),
                        round(float(row["adjusted_delta"]), 3),
                        row["star_gain"],
                        row["flags"] or "",
                    ])
            else:
                raw_sheet.append([
                    "-",
                    "No attacks",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                ])
            raw_name = (
                "Raw Attacks"
                if len(selected_clans) == 1
                else f"{clan_code}_Raw"
            )
            workbook_sheets.append((raw_name, raw_sheet))
        return workbook_sheets
