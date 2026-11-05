"""Player-health export command."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any, Dict, List, Optional

import discord

from elbow_helper.infrastructure.discord.interactions import deny, warn

from elbow_helper.domain.player_tags import normalize_player_tag

from ..config import UTC
from ..export import HealthWorkbookError, PreparedHealthExport
from ..analysis.verdicts import (
    GOOD,
    INSUFFICIENT_DATA,
    NEEDS_REVIEW,
    NOT_TRACKED,
    WATCH,
    aggregate_overall_verdict,
    normalize_player_verdict,
    trend_from_verdicts,
)
from ..record_wars import newest_attacks
from ..seasons import (
    _latest_completed_season_key,
    _parse_season_key,
    _prev_month,
    _season_end_dt,
)
from .clan import roster_row

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlayerHealthExport:
    workbook_name: str
    workbook_title: str
    summary_lines: list[str]
    sheets: list[tuple[str, list[list[Any]]]]


@dataclass(frozen=True, slots=True)
class PlayerHealthWindow:
    now: datetime
    mode: str
    season_key: str
    trend_season_key: str
    start: datetime
    end: datetime
    label: str
    date_from: str | None
    date_to: str | None


@dataclass(frozen=True, slots=True)
class PlayerHealthCommandResult:
    status: str
    issue: str = ""
    export: PreparedHealthExport | None = None
    season_key: str = ""


def prepare_player_health_window(
    mode: str, *, date_from: str | None = None,
    date_to: str | None = None, now: datetime | None = None,
) -> tuple[PlayerHealthWindow | None, str | None]:
    now = now or datetime.now(UTC)
    trend_season_key = _latest_completed_season_key(now)
    if mode == "last_7d":
        season_key, label = "last 7d", "Last 7 days"
        start, end = now - timedelta(days=7), now
    elif mode == "last_14d":
        season_key, label = "last 14d", "Last 14 days"
        start, end = now - timedelta(days=14), now
    elif mode == "custom":
        if not date_from or not date_to:
            return None, "Enter both a start date and an end date in YYYY-MM-DD format."
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return None, f"`{date_from}` isn't a valid start date. Use YYYY-MM-DD."
        try:
            end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return None, f"`{date_to}` isn't a valid end date. Use YYYY-MM-DD."
        if start >= end:
            return None, "The start date must be before the end date."
        if (end - start).days > 365:
            return None, "Choose a date range of 365 days or less."
        season_key = f"custom {date_from}..{date_to}"
        label = f"Custom: {date_from} to {date_to}"
    else:
        mode, season_key, label = "last_30d", "last 30d", "Last 30 days"
        start, end = now - timedelta(days=30), now
    return PlayerHealthWindow(now, mode, season_key, trend_season_key,
                              start, end, label, date_from, date_to), None


class ClanHealthPlayerCommandMixin:
    @staticmethod
    def _trend_summary(symbol: str, prior_windows: int) -> tuple[str, str]:
        if prior_windows < 2:
            return "Not enough history", "Two earlier completed periods are needed for comparison."
        if symbol == "^":
            return "Getting better", "Compared with the previous two completed periods."
        if symbol == "v":
            return "Getting worse", "Compared with the previous two completed periods."
        return "No change", "Compared with the previous two completed periods."

    @staticmethod
    def _signal_group_summary(cards: List[Dict[str, Any]], *, empty_note: str) -> tuple[str, str]:
        severity = {
            NEEDS_REVIEW: 3,
            WATCH: 2,
            GOOD: 1,
            INSUFFICIENT_DATA: 0,
            NOT_TRACKED: -1,
        }
        relevant = [
            card for card in cards
            if isinstance(card, dict) and normalize_player_verdict(card.get("verdict")) != NOT_TRACKED
        ]
        if not relevant:
            return INSUFFICIENT_DATA, empty_note
        ranked = sorted(
            relevant,
            key=lambda card: (
                -severity.get(normalize_player_verdict(card.get("verdict")), 0),
                str(card.get("name") or ""),
            ),
        )
        top = ranked[0]
        return (
            normalize_player_verdict(top.get("verdict")),
            ClanHealthPlayerCommandMixin._signal_summary_text(top, empty_note=empty_note),
        )

    @staticmethod
    def _signal_summary_text(card: Dict[str, Any], *, empty_note: str = "No details available.") -> str:
        name = str(card.get("name") or "").strip().lower()
        current = str(card.get("current") or "").strip()
        reason = str(card.get("reason") or "").strip()
        if name == "war participation" and reason:
            return reason
        if name == "cwl participation" and current:
            return current
        return current or reason or empty_note

    @staticmethod
    def _hit_type_label(is_fresh: Any) -> str:
        # Unknown when an earlier attack on that defender may be missing.
        if is_fresh is None:
            return "-"
        return "First hit" if bool(is_fresh) else "Cleanup"

    @staticmethod
    def _split_attack_rows(rows: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        regular = []
        cwl = []
        for row in rows:
            war_type = str(row.get("war_type") or "").strip().upper()
            if war_type == "REG":
                regular.append(row)
            elif war_type == "CWL":
                cwl.append(row)
        return regular, cwl

    @classmethod
    def _build_attack_sheet(cls, rows: List[Dict[str, Any]], empty_message: str) -> List[List[Any]]:
        sheet: List[List[Any]] = [
            ["Ended (UTC)", "Clan", "Defender", "Position", "TH", "Stars", "Destruction %", "Hit type"]
        ]
        for row in rows:
            end_ts = int(row.get("end_ts") or 0)
            sheet.append(
                [
                    datetime.fromtimestamp(end_ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC") if end_ts > 0 else "-",
                    str(row.get("clan_code") or "-"),
                    str(row.get("defender_name") or row.get("defender_tag") or "-"),
                    int(row.get("defender_map_position") or 0),
                    int(row.get("defender_townhall") or 0),
                    int(row.get("stars") or 0),
                    round(float(row.get("destruction") or 0.0), 1),
                    cls._hit_type_label(row.get("fresh_attack")),
                ]
            )
        if len(sheet) == 1:
            sheet.append([empty_message, "", "", "", "", "", "", ""])
        return sheet

    def _player_war_attacks(self, player_tag: str, cycle_start: datetime, cycle_end: datetime) -> List[Dict[str, Any]]:
        """Regular-war and league-war attacks from the family record."""
        window = {"player_tag": player_tag, "cycle_start_ts": int(cycle_start.timestamp()),
                  "cycle_end_ts": int(cycle_end.timestamp())}
        return newest_attacks(self.regular_wars.attacks(**window), self.league_wars.attacks(**window))

    @staticmethod
    def _clan_code_fallback(*rows: Dict[str, Any]) -> Optional[str]:
        for row in rows:
            code = str((row or {}).get("clan_code") or "").strip()
            if code:
                return code
        return None

    async def _export_player_health(
        self,
        interaction: discord.Interaction,
        player: str,
        window: Optional[Any] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> None:
        started = time.monotonic()
        if not self._has_access(interaction):
            LOGGER.info("Command denied /health player user=%s", getattr(interaction.user, "id", None))
            await deny(interaction)
            return
        result = await self.run_player_health_export(
            player, mode=window.value if window else "last_30d",
            date_from=date_from, date_to=date_to,
            on_valid=lambda: interaction.response.defer(thinking=True),
        )
        if result.status == "invalid_account":
            await warn(interaction, "Choose a Clash account from the list.")
            return
        if result.status == "invalid_window":
            await warn(interaction, result.issue)
            return
        if result.status == "empty":
            await interaction.followup.send("No health data is available for that player during this period.")
            return
        if result.status == "unavailable":
            await interaction.followup.send(
                "Could not generate the spreadsheet right now. Try again in a moment.",
            )
            return
        assert result.export is not None
        await self.send_prepared_health_export(interaction, result.export)
        LOGGER.debug(
            "Command done /health player user=%s tag=%s season=%s elapsed=%.2fs",
            getattr(interaction.user, "id", None),
            normalize_player_tag(player),
            result.season_key,
            time.monotonic() - started,
        )

    async def run_player_health_export(
        self, player: str, *, mode: str = "last_30d",
        date_from: str | None = None, date_to: str | None = None,
        on_valid: Callable[[], Awaitable[Any]] | None = None,
    ) -> PlayerHealthCommandResult:
        player_tag = normalize_player_tag(player)
        if not player_tag:
            return PlayerHealthCommandResult("invalid_account")
        selected, issue = prepare_player_health_window(
            mode, date_from=date_from, date_to=date_to,
        )
        if issue or selected is None:
            return PlayerHealthCommandResult("invalid_window", issue or "")
        if on_valid is not None:
            await on_valid()
        report = await self.build_player_health_export(
            player_tag=player_tag, now=selected.now, window_mode=selected.mode,
            season_key=selected.season_key,
            trend_season_key=selected.trend_season_key,
            cycle_start=selected.start, cycle_end=selected.end,
            window_label=selected.label, partial=False,
            date_from=date_from, date_to=date_to,
        )
        if report is None:
            return PlayerHealthCommandResult("empty")
        try:
            export = await self.prepare_health_export(
                workbook_name=report.workbook_name,
                workbook_title=report.workbook_title,
                summary_lines=report.summary_lines,
                sheets=report.sheets,
            )
        except HealthWorkbookError:
            return PlayerHealthCommandResult("unavailable")
        return PlayerHealthCommandResult(
            "complete", export=export, season_key=selected.season_key,
        )

    def _player_row(self, player_tag: str, cycle_start: datetime, cycle_end: datetime,
                    stays: List[Dict[str, Any]] | None = None) -> Dict[str, Any] | None:
        """The account's health row for the window, worked out from the family record; None if it knows nothing."""
        start_ts, end_ts = int(cycle_start.timestamp()), int(cycle_end.timestamp())
        if stays is None:
            stays = self.record.stays(player_tag, cycle_start_ts=start_ts, cycle_end_ts=end_ts)
        war_attack_rows = self._player_war_attacks(player_tag, cycle_start, cycle_end)
        raid_member_rows, _ = self.record.raid_lines(
            cycle_start_ts=start_ts, cycle_end_ts=end_ts, player_tags=[player_tag],
        )
        account = self.record.directory_players([player_tag]).get(player_tag) or {}
        if not account and not stays and not war_attack_rows and not raid_member_rows:
            return None
        clan_code = self._clan_code_fallback(
            account, stays[-1] if stays else {},
            war_attack_rows[0] if war_attack_rows else {},
            raid_member_rows[0] if raid_member_rows else {},
        )
        row = roster_row(clan_code or "", {
            "player_tag": player_tag,
            "player_name": (str(account.get("player_name") or "").strip()
                            or str((war_attack_rows[0] if war_attack_rows else {}).get("player_name") or "").strip()
                            or player_tag),
        })
        row["clan_code"] = clan_code
        row["clan_profile"] = self.analyzer.profile_for_clan(clan_code)
        window = {"cycle_start": cycle_start, "cycle_end": cycle_end}
        self.analyzer.apply_war_activity([row], **window)
        self.analyzer.apply_raid_activity([row], **window)
        self.analyzer.apply_donation_activity([row], **window)
        self.analyzer.apply_progression_fallback([row], **window, force=True)
        return {"row": row, "stays": stays, "war_attack_rows": war_attack_rows, "raid_member_rows": raid_member_rows}

    def _scored_player_row(self, player_tag: str, cycle_start: datetime, cycle_end: datetime,
                           stays: List[Dict[str, Any]] | None = None) -> Dict[str, Any] | None:
        found = self._player_row(player_tag, cycle_start, cycle_end, stays)
        if found is None:
            return None
        row, raid_member_rows = found["row"], found["raid_member_rows"]
        row["_war_attack_rows"] = found["war_attack_rows"]
        row["raid_weekends_joined"] = len({line["weekend_id"] for line in raid_member_rows if int(line.get("attacks") or 0) > 0})
        row["raid_weekends_window"] = len({line["weekend_id"] for line in raid_member_rows})
        self.analyzer.apply_flags([row], cycle_start=cycle_start, cycle_end=cycle_end)
        return found

    def _trend_statuses(self, player_tag: str, trend_season_key: str) -> List[str]:
        """Verdicts for the two latest completed seasons the account spent in the family, newest first.

        Worked out from the record like the report itself; seasons before the
        record's own history use the old health history copied into it.
        """
        statuses = []
        year, month = _parse_season_key(trend_season_key) or (0, 0)
        for _ in range(2):
            if not year:
                break
            previous = _prev_month(year, month)
            start, end = _season_end_dt(*previous), _season_end_dt(year, month)
            stays = self.record.stays(player_tag, cycle_start_ts=int(start.timestamp()), cycle_end_ts=int(end.timestamp()))
            if stays:
                found = self._scored_player_row(player_tag, start, end, stays)
                if found is not None:
                    statuses.append(normalize_player_verdict(found["row"].get("status")))
            year, month = previous
        return statuses

    async def build_player_health_export(
        self, *, player_tag: str, now: datetime, window_mode: str,
        season_key: str, trend_season_key: str, cycle_start: datetime,
        cycle_end: datetime, window_label: str, partial: bool = False,
        date_from: str | None = None, date_to: str | None = None,
    ) -> PlayerHealthExport | None:
        del now, partial
        if window_mode == "last_7d":
            window_label = "Last 7 days"
        elif window_mode == "last_14d":
            window_label = "Last 14 days"
        elif window_mode == "custom":
            window_label = f"Custom: {date_from} to {date_to}"
        else:
            window_label = "Last 30 days"

        found = await asyncio.to_thread(self._scored_player_row, player_tag, cycle_start, cycle_end)
        if found is None:
            LOGGER.info("Command no data /health player tag=%s season=%s", player_tag, season_key)
            return None
        report_row, war_attack_rows = found["row"], found["war_attack_rows"]
        movement_segments = found["stays"]
        history_statuses = await asyncio.to_thread(self._trend_statuses, player_tag, trend_season_key)

        player_name = report_row.get("player_name") or player_tag
        player_clan_code = report_row.get("clan_code")
        player_profile_key = str(
            report_row.get("clan_profile") or self.analyzer.profile_for_clan(player_clan_code)
        ).strip().lower()
        player_profile_label = player_profile_key.title() if player_profile_key else "Casual"

        partial_data_fields = (
            "townhall",
            "hero_sum",
            "pet_sum",
            "equipment_sum",
            "troop_sum",
            "spell_sum",
            "games_total",
            "capital_contrib",
            "war_stars",
            "attack_wins",
        )
        partial_data_notice = (
            "Some player data was unavailable; showing partial results."
            if sum(1 for field in partial_data_fields if report_row.get(field) is None) >= 3
            else None
        )

        signal_cards = report_row.get("signal_cards") or {}
        verdict_map = {
            name: str(card.get("verdict") or INSUFFICIENT_DATA)
            for name, card in signal_cards.items()
            if bool(card.get("graded"))
        }
        overall_details = report_row.get("overall_details") or aggregate_overall_verdict(
            verdict_map,
            profile_name=player_profile_key,
            return_details=True,
        )
        overall_verdict = str(overall_details["overall"])
        overall_trend, overall_streak = trend_from_verdicts([overall_verdict, *history_statuses])

        trend_label, trend_note = self._trend_summary(overall_trend, len(history_statuses))
        if overall_streak >= 2 and overall_verdict in {WATCH, NEEDS_REVIEW} and len(history_statuses) >= 2:
            trend_note = f"{trend_note} {overall_streak} periods in a row at {overall_verdict}."
        war_label, war_note = self._signal_group_summary(
            [
                signal_cards.get("war_attendance") or {},
                signal_cards.get("war_hit_usage") or {},
                signal_cards.get("cwl_participation") or {},
                signal_cards.get("cwl_hit_usage") or {},
            ],
            empty_note="No war or CWL activity is available for this period.",
        )
        raid_label, raid_note = self._signal_group_summary(
            [
                signal_cards.get("raid_participation") or {},
                signal_cards.get("raid_value") or {},
            ],
            empty_note="No Raid Weekend activity is available for this period.",
        )
        clan_games_card = signal_cards.get("clan_games") or {}
        clan_games_label = normalize_player_verdict(clan_games_card.get("verdict"))
        clan_games_note = self._signal_summary_text(
            clan_games_card,
            empty_note="No Clan Games activity is available for this player during this period.",
        )
        headline = (
            "At least one activity area needs leadership review."
            if overall_verdict == NEEDS_REVIEW
            else ("Check again after the next reporting period." if overall_verdict == WATCH else ("No action needed." if overall_verdict == GOOD else "Check again when more activity is available."))
        )

        overview_sheet = [
            ["Label", "Value", "Notes"],
            ["Player", player_name, player_tag],
            ["Clan", player_clan_code or "-", player_profile_label],
            ["Period", window_label, f"{cycle_start.date().isoformat()} to {cycle_end.date().isoformat()}"],
            ["Overall result", overall_verdict, headline],
            ["Trend", trend_label, trend_note],
            ["War / CWL", war_label, war_note],
            ["Raid Weekend", raid_label, raid_note],
            ["Clan Games", clan_games_label, clan_games_note],
        ]
        if partial_data_notice:
            overview_sheet.append(["Data note", "Partial results", partial_data_notice])

        activity_rows: List[List[Any]] = [["Area", "Result", "Activity", "Expectation", "Reason"]]
        signal_order = [
            "war_attendance",
            "war_hit_usage",
            "cwl_participation",
            "cwl_hit_usage",
            "raid_participation",
            "raid_value",
            "clan_games",
            "donations",
            "progression",
        ]
        for signal_name in signal_order:
            card = signal_cards.get(signal_name) or {}
            expectation = card.get("target") or "Not tracked"
            if str(expectation).strip().lower() == "context only":
                expectation = "Not graded"
            activity_rows.append(
                [
                    card.get("name") or signal_name.replace("_", " ").title(),
                    card.get("verdict") or INSUFFICIENT_DATA,
                    card.get("current") or "-",
                    expectation,
                    card.get("reason") or "Not enough data for this period.",
                ]
            )

        history_sheet = [["Clan", "From", "Until"]]
        for segment in sorted(movement_segments, key=lambda item: int(item.get("start_ts") or 0)):
            start_ts = int(segment.get("start_ts") or 0)
            end_ts = int(segment.get("end_ts") or 0)
            history_sheet.append(
                [
                    str(segment.get("clan_code") or "-"),
                    datetime.fromtimestamp(start_ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC") if start_ts > 0 else "-",
                    datetime.fromtimestamp(end_ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC") if end_ts > 0 else "-",
                ]
            )
        if len(history_sheet) == 1:
            history_sheet.append(["No clan history recorded", "", ""])

        regular_war_rows, cwl_rows = self._split_attack_rows(war_attack_rows)
        war_sheet = self._build_attack_sheet(
            regular_war_rows,
            "No regular-war attacks during this period",
        )
        cwl_sheet = self._build_attack_sheet(
            cwl_rows,
            "No CWL attacks during this period",
        )

        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        workbook_name = f"player_health_{player_tag.replace('#', '')}_{season_key}_{timestamp}.xlsx"
        workbook_title = f"**Player Health - {player_name} - {window_label}**"
        summary_lines = [
            f"Player Health report for `{player_name}` (`{player_tag}`).",
            f"Period: {window_label}",
            f"Clan type: {player_profile_label}",
            f"Overall result: {overall_verdict}",
        ]
        if partial_data_notice:
            summary_lines.insert(1, partial_data_notice)
        workbook_sheets: List[tuple[str, List[List[Any]]]] = [
            ("Overview", overview_sheet),
            ("Breakdown", activity_rows),
            ("Wars", war_sheet),
            ("CWL", cwl_sheet),
            ("Clan History", history_sheet),
        ]
        return PlayerHealthExport(
            workbook_name=workbook_name, workbook_title=workbook_title,
            summary_lines=summary_lines, sheets=workbook_sheets,
        )
