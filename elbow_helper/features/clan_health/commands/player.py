"""Player-health export command."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import discord

from elbow_helper.discord.interactions import deny, warn

from elbow_helper.domain.player_tags import normalize_player_tag

from ..config import UTC
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
from ..seasons import (
    _latest_completed_season_key,
)

LOGGER = logging.getLogger(__name__)


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

    @staticmethod
    def _latest_completed_raid_weekend_end(reference: datetime) -> datetime:
        ref = reference.astimezone(UTC)
        weekend_end = ref.replace(hour=7, minute=0, second=0, microsecond=0) - timedelta(days=ref.weekday())
        if weekend_end > ref:
            weekend_end -= timedelta(days=7)
        return weekend_end

    @classmethod
    def _raid_history_is_stale(
        cls,
        *,
        cycle_start: datetime,
        cycle_end: datetime,
        now: datetime,
        raid_member_rows: List[Dict[str, Any]],
    ) -> bool:
        latest_completed_end = cls._latest_completed_raid_weekend_end(now)
        if not (cycle_start <= latest_completed_end <= cycle_end):
            return False
        latest_stored_end_ts = max((int(row.get("end_ts") or 0) for row in raid_member_rows), default=0)
        if latest_stored_end_ts <= 0:
            return True
        latest_stored_end = datetime.fromtimestamp(latest_stored_end_ts, tz=UTC)
        return latest_stored_end < latest_completed_end

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

        player_tag = normalize_player_tag(player)
        if not player_tag:
            await warn(interaction, "Choose a Clash account from the list.")
            return

        now = datetime.now(UTC)
        window_mode = window.value if window else "last_30d"
        trend_season_key = _latest_completed_season_key(now)
        partial = False
        if window_mode == "last_7d":
            season_key = "last 7d"
            cycle_end = now
            cycle_start = now - timedelta(days=7)
        elif window_mode == "last_14d":
            season_key = "last 14d"
            cycle_end = now
            cycle_start = now - timedelta(days=14)
        elif window_mode == "custom":
            if not date_from or not date_to:
                await warn(interaction, "Enter both a start date and an end date in YYYY-MM-DD format.")
                return
            try:
                cycle_start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                await warn(interaction, f"`{date_from}` isn't a valid start date. Use YYYY-MM-DD.")
                return
            try:
                cycle_end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                await warn(interaction, f"`{date_to}` isn't a valid end date. Use YYYY-MM-DD.")
                return
            if cycle_start >= cycle_end:
                await warn(interaction, "The start date must be before the end date.")
                return
            if (cycle_end - cycle_start).days > 365:
                await warn(interaction, "Choose a date range of 365 days or less.")
                return
            season_key = f"custom {date_from}..{date_to}"
        else:
            season_key = "last 30d"
            cycle_end = now
            cycle_start = now - timedelta(days=30)

        await interaction.response.defer(thinking=True)

        report_lookup_season = (
            await asyncio.to_thread(
                self.repository.latest_activity_season,
                player_tag,
            )
            or trend_season_key
        )
        if window_mode == "last_7d":
            window_label = "Last 7 days"
        elif window_mode == "last_14d":
            window_label = "Last 14 days"
        elif window_mode == "custom":
            window_label = f"Custom: {date_from} to {date_to}"
        else:
            window_label = "Last 30 days"

        report_row = await asyncio.to_thread(
            self.repository.latest_player_report,
            report_lookup_season,
            player_tag,
        )
        history = await asyncio.to_thread(
            self.repository.snapshot_history,
            player_tag,
            limit=80,
        )
        if report_row:
            await asyncio.to_thread(
                self.analyzer.apply_war_activity,
                [report_row],
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            await asyncio.to_thread(
                self.analyzer.apply_raid_activity,
                [report_row],
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            await asyncio.to_thread(
                self.analyzer.apply_donation_activity,
                [report_row],
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            await asyncio.to_thread(
                self.analyzer.apply_progression_fallback,
                [report_row],
                cycle_start=cycle_start,
                cycle_end=cycle_end,
                force=True,
            )

        war_attack_rows = await asyncio.to_thread(
            self.repository.player_war_attacks,
            player_tag=player_tag,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            limit=0,
        )
        raid_member_rows = await asyncio.to_thread(
            self.repository.player_raid_activity,
            player_tag=player_tag,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            limit=0,
        )
        trend_history = await asyncio.to_thread(
            self.analyzer.player_trend_history,
            player_tag=player_tag,
            up_to_season_key=trend_season_key,
            limit=6,
        )
        movement_segments = await asyncio.to_thread(
            self.repository.player_movement,
            player_tag=player_tag,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            lookback_days=0,
        )
        clan_code = self._clan_code_fallback(
            report_row or {},
            history[0] if history else {},
            war_attack_rows[0] if war_attack_rows else {},
            raid_member_rows[0] if raid_member_rows else {},
        )
        live_warnings: List[str] = []

        stale_raid_history = self._raid_history_is_stale(
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            now=now,
            raid_member_rows=raid_member_rows,
        )
        if self.clash_client.configured and stale_raid_history and clan_code:
            _, raid_refresh_warnings = await self.collector.collect_clan_live(
                clan_code=clan_code,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            live_warnings.extend(raid_refresh_warnings)
            raid_member_rows = await asyncio.to_thread(
                self.repository.player_raid_activity,
                player_tag=player_tag,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
                limit=0,
            )
            if report_row:
                await asyncio.to_thread(
                    self.analyzer.apply_raid_activity,
                    [report_row],
                    cycle_start=cycle_start,
                    cycle_end=cycle_end,
                )

        should_try_live = self.clash_client.configured and (
            not report_row or self.analyzer.report_row_is_sparse(report_row)
        )
        if should_try_live:
            live_row, player_live_warnings = await self.collector.try_live_player_row(
                player_tag=player_tag,
                season_key=report_lookup_season,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            live_warnings.extend(player_live_warnings)
            if live_row:
                clan_code = str(live_row.get("clan_code") or clan_code or "").strip() or None
                report_row = {
                    "player_tag": player_tag,
                    "player_name": live_row.get("player_name") or player_tag,
                    "clan_code": live_row.get("clan_code"),
                    "clan_profile": live_row.get("clan_profile") or self.analyzer.profile_for_clan(live_row.get("clan_code")),
                    "status": normalize_player_verdict(live_row.get("status") or GOOD),
                    "flags_json": json.dumps(live_row.get("flags") or [], ensure_ascii=False),
                    "note": live_row.get("note") or "",
                    "war_hits_used": int(live_row.get("war_hits_used") or 0),
                    "war_hits_expected": int(live_row.get("war_hits_expected") or 0),
                    "war_missed": int(live_row.get("war_missed") or 0),
                    "war_stars_total": float(live_row.get("war_stars_total") or 0.0),
                    "war_destruction_total": float(live_row.get("war_destruction_total") or 0.0),
                    "war_attack_count": int(live_row.get("war_attack_count") or 0),
                    "raid_attacks": int(live_row.get("raid_attacks") or 0),
                    "raid_expected": int(live_row.get("raid_expected") or 0),
                    "raid_loot": int(live_row.get("raid_loot") or 0),
                    "raid_expected_estimated": 1 if live_row.get("raid_expected_estimated") else 0,
                    "donations": int(live_row.get("donations") or 0),
                    "donations_received": int(live_row.get("donations_received") or 0),
                    "trophies": int(live_row.get("trophies") or 0),
                    "war_stars": live_row.get("war_stars"),
                    "attack_wins": live_row.get("attack_wins"),
                    "capital_contrib": live_row.get("capital_contrib"),
                    "townhall": live_row.get("townhall"),
                    "hero_sum": live_row.get("hero_sum"),
                    "pet_sum": live_row.get("pet_sum"),
                    "equipment_sum": live_row.get("equipment_sum"),
                    "troop_sum": live_row.get("troop_sum"),
                    "spell_sum": live_row.get("spell_sum"),
                    "games_total": live_row.get("games_total"),
                    "hero_delta": live_row.get("hero_delta"),
                    "pet_delta": live_row.get("pet_delta"),
                    "equipment_delta": live_row.get("equipment_delta"),
                    "troop_delta": live_row.get("troop_delta"),
                    "spell_delta": live_row.get("spell_delta"),
                    "capital_delta": live_row.get("capital_delta"),
                    "th_delta": live_row.get("th_delta"),
                    "games_delta": live_row.get("games_delta"),
                    "season_key": season_key,
                }
                await asyncio.to_thread(
                    self.analyzer.apply_war_activity,
                    [report_row],
                    cycle_start=cycle_start,
                    cycle_end=cycle_end,
                )
                await asyncio.to_thread(
                    self.analyzer.apply_raid_activity,
                    [report_row],
                    cycle_start=cycle_start,
                    cycle_end=cycle_end,
                )
                await asyncio.to_thread(
                    self.analyzer.apply_donation_activity,
                    [report_row],
                    cycle_start=cycle_start,
                    cycle_end=cycle_end,
                )
                await asyncio.to_thread(
                    self.analyzer.apply_progression_fallback,
                    [report_row],
                    cycle_start=cycle_start,
                    cycle_end=cycle_end,
                    force=True,
                )

                captured_ts = int(now.timestamp())
                await asyncio.to_thread(self.repository.store_snapshots, captured_ts, [live_row])
                run_id = f"player_health:{report_lookup_season}:{player_tag}:{captured_ts}"
                await asyncio.to_thread(
                    self.repository.store_report,
                    run_id=run_id,
                    created_ts=captured_ts,
                    season_key=report_lookup_season,
                    scope="PLAYER",
                    partial=partial,
                    cycle_start_ts=int(cycle_start.timestamp()),
                    cycle_end_ts=int(cycle_end.timestamp()),
                    rows=[live_row],
                )
                history = await asyncio.to_thread(
                    self.repository.snapshot_history,
                    player_tag,
                    limit=80,
                )
                war_attack_rows = await asyncio.to_thread(
                    self.repository.player_war_attacks,
                    player_tag=player_tag,
                    cycle_start=cycle_start,
                    cycle_end=cycle_end,
                    limit=0,
                )
                raid_member_rows = await asyncio.to_thread(
                    self.repository.player_raid_activity,
                    player_tag=player_tag,
                    cycle_start=cycle_start,
                    cycle_end=cycle_end,
                    limit=0,
                )
                trend_history = await asyncio.to_thread(
                    self.analyzer.player_trend_history,
                    player_tag=player_tag,
                    up_to_season_key=trend_season_key,
                    limit=6,
                )
                movement_segments = await asyncio.to_thread(
                    self.repository.player_movement,
                    player_tag=player_tag,
                    cycle_start=cycle_start,
                    cycle_end=cycle_end,
                    lookback_days=0,
                )

        if not report_row and not history and not war_attack_rows and not raid_member_rows:
            LOGGER.info(
                "Command no data /health player tag=%s season=%s warnings=%s",
                player_tag,
                season_key,
                live_warnings,
            )
            await interaction.followup.send("No health data is available for that player during this period.")
            return

        if not report_row:
            seed_history = history[0] if history else {}
            seed_war = war_attack_rows[0] if war_attack_rows else {}
            seed_raid = raid_member_rows[0] if raid_member_rows else {}
            seed_clan_code = self._clan_code_fallback(seed_history, seed_war, seed_raid)
            seed_player_name = (
                str(seed_history.get("player_name") or "").strip()
                or str(seed_war.get("player_name") or "").strip()
                or str(seed_raid.get("player_name") or "").strip()
                or player_tag
            )
            report_row = {
                "player_tag": player_tag,
                "player_name": seed_player_name,
                "clan_code": seed_clan_code,
                "clan_profile": self.analyzer.profile_for_clan(seed_clan_code),
                "status": GOOD,
                "flags_json": "[]",
                "note": "",
                "war_hits_used": 0,
                "war_hits_expected": 0,
                "war_missed": 0,
                "war_stars_total": 0.0,
                "war_destruction_total": 0.0,
                "war_attack_count": 0,
                "raid_attacks": 0,
                "raid_expected": 0,
                "raid_loot": 0,
                "raid_expected_estimated": 0,
                "donations": int(seed_history.get("donations") or 0),
                "donations_received": int(seed_history.get("donations_received") or 0),
                "trophies": int(seed_history.get("trophies") or 0),
                "war_stars": seed_history.get("war_stars"),
                "attack_wins": seed_history.get("attack_wins"),
                "capital_contrib": seed_history.get("capital_contrib"),
                "townhall": seed_history.get("townhall"),
                "hero_sum": seed_history.get("hero_sum"),
                "pet_sum": seed_history.get("pet_sum"),
                "equipment_sum": seed_history.get("equipment_sum"),
                "troop_sum": seed_history.get("troop_sum"),
                "spell_sum": seed_history.get("spell_sum"),
                "games_total": seed_history.get("games_total"),
                "hero_delta": None,
                "pet_delta": None,
                "equipment_delta": None,
                "troop_delta": None,
                "spell_delta": None,
                "capital_delta": None,
                "th_delta": None,
                "games_delta": None,
            }
            await asyncio.to_thread(
                self.analyzer.apply_war_activity,
                [report_row],
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            await asyncio.to_thread(
                self.analyzer.apply_raid_activity,
                [report_row],
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            await asyncio.to_thread(
                self.analyzer.apply_donation_activity,
                [report_row],
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            await asyncio.to_thread(
                self.analyzer.apply_progression_fallback,
                [report_row],
                cycle_start=cycle_start,
                cycle_end=cycle_end,
                force=True,
            )

        player_name = report_row.get("player_name") if report_row else (history[0].get("player_name") if history else player_tag)

        player_clan_code = (report_row.get("clan_code") if report_row else None) or (history[0].get("clan_code") if history else None)
        player_profile_key = str(
            (report_row.get("clan_profile") if report_row else None) or self.analyzer.profile_for_clan(player_clan_code)
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
            if report_row and sum(1 for field in partial_data_fields if report_row.get(field) is None) >= 3
            else None
        )

        if report_row:
            report_row["_war_attack_rows"] = war_attack_rows
            report_row["raid_weekends_joined"] = len({row.get("weekend_id") for row in raid_member_rows if row.get("weekend_id") and int(row.get("attacks") or 0) > 0})
            report_row["raid_weekends_window"] = len({row.get("weekend_id") for row in raid_member_rows if row.get("weekend_id")})
            if movement_segments:
                current_history = next(
                    (seg for seg in movement_segments if str(seg.get("clan_code") or "") == str(player_clan_code or "")),
                    movement_segments[0],
                )
                report_row["current_clan_history"] = current_history
            await asyncio.to_thread(
                self.analyzer.apply_flags,
                [report_row],
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )

        signal_cards = (report_row or {}).get("signal_cards") or {}
        verdict_map = {
            name: str(card.get("verdict") or INSUFFICIENT_DATA)
            for name, card in signal_cards.items()
            if bool(card.get("graded"))
        }
        overall_details = (report_row or {}).get("overall_details") or aggregate_overall_verdict(
            verdict_map,
            profile_name=player_profile_key,
            return_details=True,
        )
        overall_verdict = str(overall_details["overall"])
        history_statuses = [normalize_player_verdict(hist.get("status")) for hist in trend_history[:2]]
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
        await self._write_and_send_export(
            interaction=interaction,
            workbook_name=workbook_name,
            workbook_title=workbook_title,
            summary_lines=summary_lines,
            sheets=workbook_sheets,
        )
        LOGGER.debug(
            "Command done /health player user=%s tag=%s season=%s elapsed=%.2fs",
            getattr(interaction.user, "id", None),
            player_tag,
            season_key,
            time.monotonic() - started,
        )
