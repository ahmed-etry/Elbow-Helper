
"""Overlay and trend helper logic for clan-health scoring."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from ..player_health_config import effective_player_rules, load_player_health_config, profile_health_settings, raid_scoring_enabled
from ..seasons import _parse_season_key
from .verdicts import clan_games_assessment, progression_assessment, support_assessment

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
        latest_map = self.repository.latest_snapshots(
            cutoff_ts=cycle_end_ts,
            player_tags=needs,
        )
        baseline_map = self.repository.earliest_snapshots(
            cycle_start_ts=cycle_start_ts,
            cycle_end_ts=cycle_end_ts,
            player_tags=needs,
        )
        baseline_fallback_cache: Dict[str, Optional[sqlite3.Row]] = {}
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
            if baseline is None:
                if tag not in baseline_fallback_cache:
                    baseline_fallback_cache[tag] = self.repository.baseline_snapshot(tag, cycle_start_ts)
                baseline = baseline_fallback_cache[tag]
            if not baseline:
                continue
            def baseline_value(key: str) -> Optional[int]:
                if isinstance(baseline, sqlite3.Row):
                    return baseline[key]
                return (baseline or {}).get(key)

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
        aggregate = self.repository.war_activity(
            cycle_start_ts=int(cycle_start.timestamp()),
            cycle_end_ts=int(cycle_end.timestamp()),
            player_tags=player_tags,
        )
        for row in rows:
            tag = str(row.get("player_tag") or "")
            stats = aggregate.get(tag)
            if not stats:
                row["war_events_joined"] = 0
                continue
            # War overlays replace timeframe war fields so scoring always uses family-wide logs.
            row["war_events_joined"] = int(stats.get("war_events_joined") or 0)
            expected = int(stats.get("attacks_expected") or 0)
            used = int(stats.get("attacks_used") or 0)
            row["war_hits_expected"] = expected
            row["war_hits_used"] = used
            row["war_missed"] = max(0, expected - used)
            row["war_stars_total"] = float(stats.get("stars") or 0.0)
            row["war_destruction_total"] = float(stats.get("destruction") or 0.0)
            row["war_attack_count"] = int(stats.get("attack_count") or 0)
            row["regular_war_events_joined"] = int(stats.get("regular_war_events_joined") or 0)
            row["regular_war_hits_expected"] = int(stats.get("regular_attacks_expected") or 0)
            row["regular_war_hits_used"] = int(stats.get("regular_attacks_used") or 0)
            row["regular_war_missed"] = max(
                0,
                int(stats.get("regular_attacks_expected") or 0) - int(stats.get("regular_attacks_used") or 0),
            )
            row["cwl_events_joined"] = int(stats.get("cwl_events_joined") or 0)
            row["cwl_hits_expected"] = int(stats.get("cwl_attacks_expected") or 0)
            row["cwl_hits_used"] = int(stats.get("cwl_attacks_used") or 0)
            row["cwl_missed"] = max(
                0,
                int(stats.get("cwl_attacks_expected") or 0) - int(stats.get("cwl_attacks_used") or 0),
            )

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
        cycle_start_ts = int(cycle_start.timestamp())
        cycle_end_ts = int(cycle_end.timestamp())
        aggregate = self.repository.raid_activity(
            cycle_start_ts=cycle_start_ts,
            cycle_end_ts=cycle_end_ts,
            player_tags=player_tags,
        )
        weekends_in_window = self.repository.raid_weekend_count(
            cycle_start_ts=cycle_start_ts,
            cycle_end_ts=cycle_end_ts,
        )
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
        aggregate = self.repository.snapshot_counters(
            cycle_start_ts=int(cycle_start.timestamp()),
            cycle_end_ts=int(cycle_end.timestamp()),
            player_tags=player_tags,
        )
        for row in rows:
            tag = str(row.get("player_tag") or "")
            stats = aggregate.get(tag)
            if not stats:
                continue
            row["donations"] = int(stats.get("donations") or 0)
            row["donations_received"] = int(stats.get("donations_received") or 0)

    @staticmethod
    def _season_key_sortable(season_key: str) -> Tuple[int, int]:
        parsed = _parse_season_key(str(season_key or ""))
        if not parsed:
            return (0, 0)
        return parsed

    @staticmethod
    def _consistency_score_from_row(row: Dict[str, Any]) -> Optional[float]:
        clan_code = str(row.get("clan_code") or "").upper()
        if clan_code:
            rules = effective_player_rules(clan_code)
        else:
            config, errors = load_player_health_config()
            if config is None:
                raise RuntimeError("player_health_config invalid: " + " | ".join(errors[:5]))
            profile_name = str(row.get("clan_profile") or "casual").strip().lower()
            rules = profile_health_settings(config, profile_name)
        raid_rules = rules.get("raids", {})
        clan_games_rules = rules.get("clan_games", {})
        parts: List[float] = []
        expected = int(row.get("war_hits_expected") or 0)
        used = int(row.get("war_hits_used") or 0)
        if expected > 0:
            parts.append(max(0.0, min(1.0, used / expected)))

        raid_used = int(row.get("raid_attacks") or 0)
        raid_loot = int(row.get("raid_loot") or 0)
        weekends_joined = int(row.get("raid_weekends_joined") or 0)
        weekends_available = int(row.get("raid_weekends_window") or 0)
        if weekends_available > 0:
            parts.append(max(0.0, min(1.0, weekends_joined / weekends_available)))
            minimum_gold = max(0, int(raid_rules.get("minimum_capital_gold_per_event") or 0))
            if minimum_gold > 0 and weekends_joined > 0 and raid_used > 0:
                avg_gold = raid_loot / max(1, weekends_joined)
                parts.append(max(0.0, min(1.0, avg_gold / minimum_gold)))

        clan_games_result = clan_games_assessment(
            clan_games_rules=clan_games_rules,
            cg_delta=row.get("games_delta"),
            window_state="Available",
        )
        target_points = int(clan_games_result.get("target_points") or 0)
        delta_points = clan_games_result.get("delta")
        if target_points > 0 and delta_points is not None:
            parts.append(max(0.0, min(1.0, int(delta_points) / target_points)))

        progression_result = progression_assessment(
            th_delta=row.get("th_delta"),
            hero_delta=row.get("hero_delta"),
            pet_delta=row.get("pet_delta"),
            equipment_delta=row.get("equipment_delta"),
            troop_delta=row.get("troop_delta"),
            spell_delta=row.get("spell_delta"),
        )
        if progression_result["has_progression_data"]:
            parts.append(1.0 if progression_result["meaningful_progress"] else 0.5)

        support_result = support_assessment(
            donations=row.get("donations"),
            received=row.get("donations_received"),
            clan_code=clan_code,
        )
        if support_result["evidence_state"] == "full":
            parts.append(1.0 if support_result["state"] == "OK" else 0.5)
        if len(parts) < 2:
            return None
        # Core consistency score uses only comparable cross-window components.
        return round((sum(parts) / len(parts)) * 100.0, 1)

    @staticmethod
    def _report_row_is_sparse(row: Optional[Dict[str, Any]]) -> bool:
        if not row:
            return True
        war_expected = int(row.get("war_hits_expected") or 0)
        war_used = int(row.get("war_hits_used") or 0)
        raid_expected = int(row.get("raid_expected") or 0)
        raid_used = int(row.get("raid_attacks") or 0)
        donations = int(row.get("donations") or 0)
        received = int(row.get("donations_received") or 0)
        has_delta = (
            row.get("games_delta") is not None
            or row.get("hero_delta") is not None
            or row.get("th_delta") is not None
        )
        # Rows with no participation, no activity, and no deltas are treated as sparse.
        return (
            war_expected == 0
            and war_used == 0
            and raid_expected == 0
            and raid_used == 0
            and donations == 0
            and received == 0
            and not has_delta
        )
