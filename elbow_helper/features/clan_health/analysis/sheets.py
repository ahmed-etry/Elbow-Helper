"""Workbook sheet assembly for clan-health exports."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Tuple

from ..seasons import _clan_games_signal_open
from .verdicts import (
    CLAN_HEALTHY,
    GOOD,
    INSUFFICIENT_DATA,
    NEEDS_REVIEW,
    WATCH,
    clan_action_summary,
    normalize_clan_verdict,
    normalize_player_verdict,
    worst_player_verdict,
)


class ClanHealthSheetMixin:
    def _build_sheets(
        self,
        selected_clans: List[str],
        clan_entries: List[Dict[str, Any]],
        cycle_start: datetime,
        cycle_end: datetime,
    ) -> Tuple[List[Tuple[str, List[List[Any]]]], List[Dict[str, Any]], Dict[str, Any]]:
        by_code = {entry["clan_code"]: entry for entry in clan_entries}
        ordered = [by_code[code] for code in selected_clans if code in by_code]
        all_rows: List[Dict[str, Any]] = []
        for entry in ordered:
            all_rows.extend(entry.get("players", []))

        for row in all_rows:
            if bool(row.get("raid_expected_estimated")) and int(row.get("raid_attacks") or 0) <= 0:
                row["raid_expected"] = 0

        cg_signal_enabled = _clan_games_signal_open(cycle_start=cycle_start, cycle_end=cycle_end)
        for row in all_rows:
            row["cg_signal_disabled"] = not bool(cg_signal_enabled)

        self._apply_family_war_activity(all_rows, cycle_start=cycle_start, cycle_end=cycle_end)
        self._apply_family_raid_activity(all_rows, cycle_start=cycle_start, cycle_end=cycle_end)
        self._apply_family_donation_activity(all_rows, cycle_start=cycle_start, cycle_end=cycle_end)
        self._apply_progression_delta_fallback(all_rows, cycle_start=cycle_start, cycle_end=cycle_end, force=True)
        self._apply_flags(all_rows, cycle_start=cycle_start, cycle_end=cycle_end)

        clan_signals_by_code: Dict[str, Dict[str, Any]] = {}
        for entry in ordered:
            code = entry["clan_code"]
            clan_signals_by_code[code] = self._compute_clan_signals(
                clan_code=code,
                players=entry.get("players", []),
            )

        sheets = [
            ("Overview", self._build_issue_sheet(selected_clans=selected_clans, all_rows=all_rows)),
            ("War Log", self._build_war_sheet(selected_clans=selected_clans, all_rows=all_rows)),
            ("Activity", self._build_activity_sheet(selected_clans=selected_clans, all_rows=all_rows)),
        ]
        overall_totals = self._summarize_overall(clan_signals_by_code)
        return sheets, all_rows, overall_totals

    def _compute_clan_signals(
        self,
        *,
        clan_code: str,
        players: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        total = len(players)
        overall_good = sum(1 for row in players if str(row.get("status") or "") == GOOD)
        overall_watch = sum(1 for row in players if str(row.get("status") or "") == WATCH)
        overall_needs = sum(1 for row in players if str(row.get("status") or "") == NEEDS_REVIEW)
        overall_ungraded = max(0, total - overall_good - overall_watch - overall_needs)
        if overall_needs > 0:
            overall = NEEDS_REVIEW
        elif overall_watch > 0:
            overall = WATCH
        elif total > 0:
            overall = CLAN_HEALTHY
        else:
            overall = INSUFFICIENT_DATA

        needs_review_names = [
            str(row.get("player_name") or "").strip()
            for row in players
            if str(row.get("status") or "") == NEEDS_REVIEW
        ]
        issue_counts: Dict[str, int] = {}
        for row in players:
            for flag in row.get("flags") or []:
                issue_counts[str(flag)] = issue_counts.get(str(flag), 0) + 1
        top_issue = max(issue_counts.items(), key=lambda item: item[1])[0] if issue_counts else "No active issues"
        return {
            "profile": self._profile_for_clan(clan_code),
            "total": total,
            "overall": normalize_clan_verdict(overall),
            "counts": {
                CLAN_HEALTHY: overall_good,
                WATCH: overall_watch,
                NEEDS_REVIEW: overall_needs,
                INSUFFICIENT_DATA: overall_ungraded,
            },
            "action_summary": clan_action_summary(needs_review_names=needs_review_names, watch_count=overall_watch),
            "headline": f"Top issue: {top_issue}" if issue_counts else "No active issues",
            "issue_counts": issue_counts,
        }

    @staticmethod
    def _summarize_overall(clan_signals_by_code: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        per_clan = {code: data.get("overall") for code, data in clan_signals_by_code.items()}
        worst = (
            NEEDS_REVIEW
            if any(v == NEEDS_REVIEW for v in per_clan.values())
            else WATCH
            if any(v == WATCH for v in per_clan.values())
            else CLAN_HEALTHY
        )
        return {"per_clan": per_clan, "worst": worst}

    @staticmethod
    def _player_name(row: Dict[str, Any]) -> str:
        return str(row.get("player_name") or "").strip() or "-"

    @staticmethod
    def _townhall(row: Dict[str, Any]) -> Any:
        townhall = int(row.get("townhall") or 0)
        return townhall if townhall > 0 else "-"

    @staticmethod
    def _status_badge(row: Dict[str, Any]) -> str:
        status = str(row.get("status") or "")
        if status in {GOOD, WATCH, NEEDS_REVIEW}:
            return status
        return INSUFFICIENT_DATA

    @staticmethod
    def _top_issue_key(row: Dict[str, Any]) -> str:
        flagged = row.get("flag_details") or []
        if not flagged:
            return ""
        _, name, _ = flagged[0]
        return str(name or "").lower()

    @staticmethod
    def _signal_verdict(card: Dict[str, Any] | None) -> str:
        if not isinstance(card, dict):
            return INSUFFICIENT_DATA
        return normalize_player_verdict(card.get("verdict"))

    def _combined_signal_verdict(self, *cards: Dict[str, Any] | None) -> str:
        verdicts = [self._signal_verdict(card) for card in cards]
        return worst_player_verdict(verdicts, ignore=set(), default=INSUFFICIENT_DATA)

    def _build_issue_sheet(self, *, selected_clans: List[str], all_rows: List[Dict[str, Any]]) -> List[List[Any]]:
        header = ["Result", "Player", "TH", "Clan", "Why"]
        rows: List[List[Any]] = [header]
        sort_key = {code: index for index, code in enumerate(selected_clans)}
        display_order = {GOOD: 0, WATCH: 1, NEEDS_REVIEW: 2, INSUFFICIENT_DATA: 3}
        sorted_rows = sorted(
            all_rows,
            key=lambda row: (
                sort_key.get(str(row.get("clan_code") or ""), 99),
                display_order.get(self._status_badge(row), 4),
                self._top_issue_key(row),
                str(row.get("player_name") or "").lower(),
            ),
        )
        for row in sorted_rows:
            status = self._status_badge(row)
            if status in {WATCH, NEEDS_REVIEW}:
                flagged = row.get("flag_details") or []
                if flagged:
                    _, _, top_detail = flagged[0]
                    why = str(top_detail or "").strip() or "No action needed"
                else:
                    why = "No action needed"
            else:
                why = "No action needed"
            rows.append(
                [
                    self._status_badge(row),
                    self._player_name(row),
                    self._townhall(row),
                    row.get("clan_code") or "-",
                    why,
                ]
            )
        if not any(len(r) > 0 and r[0] in {GOOD, WATCH, NEEDS_REVIEW, INSUFFICIENT_DATA} for r in rows):
            rows.append([GOOD, "No members on the roster", "-", "-", "No action needed"])
        return rows

    def _build_war_sheet(self, *, selected_clans: List[str], all_rows: List[Dict[str, Any]]) -> List[List[Any]]:
        header = ["Result", "Player", "TH", "Clan", "Wars", "War attacks"]
        rows = [header]
        sort_key = {code: index for index, code in enumerate(selected_clans)}
        display_order = {GOOD: 0, WATCH: 1, NEEDS_REVIEW: 2, INSUFFICIENT_DATA: 3}
        decorated = []
        for row in all_rows:
            reg_part = (row.get("signal_cards") or {}).get("war_attendance") or {}
            reg_hits = (row.get("signal_cards") or {}).get("war_hit_usage") or {}
            cwl_part = (row.get("signal_cards") or {}).get("cwl_participation") or {}
            cwl_hits = (row.get("signal_cards") or {}).get("cwl_hit_usage") or {}
            verdict = self._combined_signal_verdict(reg_part, reg_hits, cwl_part, cwl_hits)
            decorated.append((row, verdict, reg_part, reg_hits, cwl_part, cwl_hits))
        decorated.sort(
            key=lambda item: (
                sort_key.get(str(item[0].get("clan_code") or ""), 99),
                display_order.get(item[1], 4),
                self._top_issue_key(item[0]),
                str(item[0].get("player_name") or "").lower(),
            )
        )
        for row, verdict, reg_part, reg_hits, cwl_part, cwl_hits in decorated:
            reg_joined = int(row.get("regular_war_events_joined") or 0)
            cwl_joined = int(row.get("cwl_events_joined") or 0)
            reg_expected = int(row.get("regular_war_hits_expected") or 0)
            reg_used = int(row.get("regular_war_hits_used") or 0)
            cwl_expected = int(row.get("cwl_hits_expected") or 0)
            cwl_used = int(row.get("cwl_hits_used") or 0)
            total_joined = reg_joined + cwl_joined
            total_expected = reg_expected + cwl_expected
            total_used = reg_used + cwl_used
            total_missed = max(0, total_expected - total_used)
            if total_expected > 0:
                attack_word = "attack" if total_expected == 1 else "attacks"
                attacks_cell = f"Missed {total_missed} of {total_expected} war {attack_word}"
            else:
                attacks_cell = "No war attacks during this period"
            if total_joined == 1:
                wars_cell = f"Joined 1 war ({reg_joined} regular, {cwl_joined} CWL)"
            elif total_joined > 1:
                wars_cell = f"Joined {total_joined} wars ({reg_joined} regular, {cwl_joined} CWL)"
            else:
                wars_cell = "No wars joined during this period"
            rows.append(
                [
                    verdict,
                    self._player_name(row),
                    self._townhall(row),
                    row.get("clan_code") or "-",
                    wars_cell,
                    attacks_cell,
                ]
            )
        return rows

    @staticmethod
    def _progression_summary(row: Dict[str, Any]) -> str:
        fields = (
            ("TH", "th_delta"),
            ("Heroes", "hero_delta"),
            ("Pets", "pet_delta"),
            ("Gear", "equipment_delta"),
            ("Troops", "troop_delta"),
            ("Spells", "spell_delta"),
        )
        available = [(label, int(row[key])) for label, key in fields if row.get(key) is not None]
        if not available:
            return "-"
        changed = [(label, value) for label, value in available if value != 0]
        if not changed:
            return "0"
        return ", ".join(f"{label} {value:+d}" for label, value in changed)

    def _build_activity_sheet(self, *, selected_clans: List[str], all_rows: List[Dict[str, Any]]) -> List[List[Any]]:
        header = [
            "Player",
            "TH",
            "Clan",
            "Raid Weekends",
            "Raid attacks",
            "Capital gold",
            "Clan Games points",
            "Donated",
            "Received",
            "Progression",
        ]
        rows = [header]
        sort_key = {code: index for index, code in enumerate(selected_clans)}
        sorted_rows = sorted(
            all_rows,
            key=lambda row: (
                sort_key.get(str(row.get("clan_code") or ""), 99),
                -int(row.get("townhall") or 0),
                str(row.get("player_name") or "").lower(),
                str(row.get("player_tag") or ""),
            )
        )
        for row in sorted_rows:
            weekends_joined = int(row.get("raid_weekends_joined") or 0)
            weekends_window = int(row.get("raid_weekends_window") or 0)
            rows.append(
                [
                    self._player_name(row),
                    self._townhall(row),
                    row.get("clan_code") or "-",
                    f"{weekends_joined}/{weekends_window}" if weekends_window > 0 else "-",
                    int(row.get("raid_attacks") or 0),
                    int(row.get("raid_loot") or 0),
                    int(row["games_delta"]) if row.get("games_delta") is not None else "-",
                    int(row.get("donations") or 0),
                    int(row.get("donations_received") or 0),
                    self._progression_summary(row),
                ]
            )
        return rows
