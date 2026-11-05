
"""Activity overlays for clan-health scoring, from the family record."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from ..player_health_config import effective_player_rules, raid_scoring_enabled


class ClanHealthOverlayMixin:
    @staticmethod
    def _delta_or_none(current: Any, baseline: Any) -> Optional[int]:
        if current is None or baseline is None:
            return None
        return int(current) - int(baseline)

    def _apply_progression_delta_fallback(
        self,
        rows: List[Dict[str, Any]],
        *,
        cycle_start: datetime,
        cycle_end: datetime,
        force: bool = False,
    ) -> None:
        if not rows:
            return
        cycle_start_ts = int(cycle_start.timestamp())
        cycle_end_ts = int(cycle_end.timestamp())
        needs: Set[str] = {
            str(r.get("player_tag") or "")
            for r in rows
            if str(r.get("player_tag") or "")
            and (
                force
                or any(
                    r.get(key) is None
                    for key in (
                        "th_delta",
                        "hero_delta",
                        "pet_delta",
                        "equipment_delta",
                        "troop_delta",
                        "spell_delta",
                        "capital_delta",
                        "games_delta",
                    )
                )
            )
        }
        if not needs:
            return
        # Progress at the end, against the start (or, without anything by then, the first values in the window).
        latest_map = self.record.progress(needs, at_ts=cycle_end_ts)
        baseline_map = self.record.progress(needs, at_ts=cycle_start_ts, until_ts=cycle_end_ts)
        for row in rows:
            tag = str(row.get("player_tag") or "")
            if not tag:
                continue
            if (not force) and all(
                row.get(key) is not None
                for key in (
                    "th_delta",
                    "hero_delta",
                    "pet_delta",
                    "equipment_delta",
                    "troop_delta",
                    "spell_delta",
                    "capital_delta",
                    "games_delta",
                )
            ):
                continue
            latest = latest_map.get(tag)
            if latest:
                for row_key, snap_key in (
                    ("townhall", "townhall"),
                    ("hero_sum", "hero_sum"),
                    ("pet_sum", "pet_sum"),
                    ("equipment_sum", "equipment_sum"),
                    ("troop_sum", "troop_sum"),
                    ("spell_sum", "spell_sum"),
                    ("games_total", "games_total"),
                    ("capital_contrib", "capital_contrib"),
                ):
                    value = row.get(row_key)
                    latest_value = latest.get(snap_key)
                    if latest_value is None:
                        continue
                    if value is None:
                        row[row_key] = int(latest_value)
                    elif force and value == 0 and int(latest_value or 0) > 0:
                        row[row_key] = int(latest_value)

            baseline = baseline_map.get(tag)
            if not baseline:
                continue

            def baseline_value(key: str) -> Optional[int]:
                return baseline.get(key)

            row["th_delta"] = self._delta_or_none(row.get("townhall"), baseline_value("townhall"))
            row["hero_delta"] = self._delta_or_none(row.get("hero_sum"), baseline_value("hero_sum"))
            row["pet_delta"] = self._delta_or_none(row.get("pet_sum"), baseline_value("pet_sum"))
            row["equipment_delta"] = self._delta_or_none(row.get("equipment_sum"), baseline_value("equipment_sum"))
            row["troop_delta"] = self._delta_or_none(row.get("troop_sum"), baseline_value("troop_sum"))
            row["spell_delta"] = self._delta_or_none(row.get("spell_sum"), baseline_value("spell_sum"))
            row["capital_delta"] = self._delta_or_none(row.get("capital_contrib"), baseline_value("capital_contrib"))
            row["games_delta"] = self._delta_or_none(row.get("games_total"), baseline_value("games_total"))

    def _apply_family_war_activity(
        self,
        rows: List[Dict[str, Any]],
        *,
        cycle_start: datetime,
        cycle_end: datetime,
    ) -> None:
        if not rows:
            return
        player_tags: Set[str] = {
            str(r.get("player_tag") or "")
            for r in rows
            if str(r.get("player_tag") or "")
        }
        if not player_tags:
            return
        window = {
            "cycle_start_ts": int(cycle_start.timestamp()),
            "cycle_end_ts": int(cycle_end.timestamp()),
        }
        # Regular and league wars both come from the family record; league wars count the final lineup.
        regular = self.regular_wars.activity(**window, player_tags=player_tags)
        league = self.league_wars.activity(**window, player_tags=player_tags)
        for row in rows:
            tag = str(row.get("player_tag") or "")
            reg, cwl = regular.get(tag), league.get(tag)
            if not reg and not cwl:
                row["war_events_joined"] = 0
                continue
            reg, cwl = reg or {}, cwl or {}
            # War overlays replace timeframe war fields so scoring always uses family-wide logs.
            # A war whose counts the record does not know adds no expected or used attacks.
            reg_joined = int(reg.get("wars") or 0)
            reg_expected = int(reg.get("attacks_expected") or 0)
            reg_used = int(reg.get("attacks_used") or 0)
            cwl_joined = int(cwl.get("wars") or 0)
            cwl_expected = int(cwl.get("attacks_expected") or 0)
            cwl_used = int(cwl.get("attacks_used") or 0)
            expected = reg_expected + cwl_expected
            used = reg_used + cwl_used
            row["war_events_joined"] = reg_joined + cwl_joined
            row["war_hits_expected"] = expected
            row["war_hits_used"] = used
            row["war_missed"] = max(0, expected - used)
            # Stars and destruction cover the stored attacks, which the attack count counts.
            row["war_stars_total"] = float(reg.get("stars") or 0) + float(cwl.get("stars") or 0.0)
            row["war_destruction_total"] = float(reg.get("destruction") or 0) + float(cwl.get("destruction") or 0.0)
            row["war_attack_count"] = int(reg.get("attacks_measured") or 0) + int(cwl.get("attacks_measured") or 0)
            row["regular_war_events_joined"] = reg_joined
            row["regular_war_hits_expected"] = reg_expected
            row["regular_war_hits_used"] = reg_used
            row["regular_war_missed"] = max(0, reg_expected - reg_used)
            row["cwl_events_joined"] = cwl_joined
            row["cwl_hits_expected"] = cwl_expected
            row["cwl_hits_used"] = cwl_used
            row["cwl_missed"] = max(0, cwl_expected - cwl_used)

    def _apply_family_raid_activity(
        self,
        rows: List[Dict[str, Any]],
        *,
        cycle_start: datetime,
        cycle_end: datetime,
    ) -> None:
        if not rows:
            return
        player_tags: Set[str] = {
            str(r.get("player_tag") or "")
            for r in rows
            if str(r.get("player_tag") or "")
        }
        if not player_tags:
            return
        aggregate, weekends_in_window = self.record.raid_activity(
            cycle_start_ts=int(cycle_start.timestamp()),
            cycle_end_ts=int(cycle_end.timestamp()),
            player_tags=player_tags,
        )
        # Today's expectation rule: six attacks for each weekend that ended in the window.
        standardized_expected = max(0, int(weekends_in_window) * 6)
        for row in rows:
            tag = str(row.get("player_tag") or "")
            stats = aggregate.get(tag)
            weekends_participated = int(stats.get("raid_weekends_participated") or 0) if stats else 0
            row["raid_attacks"] = int(stats.get("attacks") or 0) if stats else 0
            row["raid_loot"] = int(stats.get("loot") or 0) if stats else 0
            row["raid_weekends_joined"] = weekends_participated
            row["raid_missed_weekends"] = max(0, int(weekends_in_window) - weekends_participated)
            raid_enabled = raid_scoring_enabled(effective_player_rules(str(row.get("clan_code") or "")))
            if not raid_enabled:
                row["raid_expected"] = 0
                row["raid_expected_estimated"] = False
            else:
                row["raid_expected"] = standardized_expected
                row["raid_expected_estimated"] = False
            row["raid_weekends_window"] = int(weekends_in_window)

    def _apply_family_donation_activity(
        self,
        rows: List[Dict[str, Any]],
        *,
        cycle_start: datetime,
        cycle_end: datetime,
    ) -> None:
        if not rows:
            return
        player_tags: Set[str] = {
            str(r.get("player_tag") or "")
            for r in rows
            if str(r.get("player_tag") or "")
        }
        if not player_tags:
            return
        aggregate = self.record.donations(
            player_tags,
            cycle_start_ts=int(cycle_start.timestamp()),
            cycle_end_ts=int(cycle_end.timestamp()),
        )
        for row in rows:
            tag = str(row.get("player_tag") or "")
            stats = aggregate.get(tag)
            # Only samples inside the window replace what the row already holds.
            if not stats or not stats.get("samples"):
                continue
            row["donations"] = int(stats.get("donations") or 0)
            row["donations_received"] = int(stats.get("donations_received") or 0)
