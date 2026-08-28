"""Clan Health storage composition and supported read/write contract."""

from __future__ import annotations

from pathlib import Path

from ..config import DB_PATH
from .aggregates import ClanHealthAggregates
from .cwl import ClanHealthCwlReads
from .history import ClanHealthHistory
from .records import ClanHealthRecords
from .schema import ClanHealthSchema


class ClanHealthRepository(
    ClanHealthSchema,
    ClanHealthRecords,
    ClanHealthAggregates,
    ClanHealthHistory,
    ClanHealthCwlReads,
):
    """Own all SQL and stored-shape conversion for Clan Health data."""

    def __init__(self, path: Path = DB_PATH):
        self.path = path

    initialize = ClanHealthSchema._init_db
    store_snapshots = ClanHealthRecords._store_snapshots
    store_report = ClanHealthRecords._store_report
    store_war_activity = ClanHealthRecords._store_war_activity_rows
    store_wars = ClanHealthRecords._store_war_rows
    store_final_war_rosters = (
        ClanHealthRecords._store_final_war_roster_rows
    )
    store_war_attacks = ClanHealthRecords._store_war_attack_rows
    store_raid_activity = (
        ClanHealthRecords._store_raid_member_activity_rows
    )
    latest_report_before = (
        ClanHealthRecords._load_latest_stored_report_before_ts
    )
    latest_player_report = (
        ClanHealthRecords._load_latest_player_report_row
    )

    baseline_snapshot = ClanHealthAggregates._get_baseline_snapshot
    war_activity = ClanHealthAggregates._load_war_activity_breakdown
    clan_war_counts = (
        ClanHealthAggregates._load_clan_war_window_counts_by_type
    )
    raid_weekend_count = (
        ClanHealthAggregates._count_raid_weekends_in_window
    )
    raid_activity = ClanHealthAggregates._load_raid_activity_aggregate
    snapshot_counters = (
        ClanHealthAggregates._load_snapshot_counter_aggregate
    )
    earliest_snapshots = (
        ClanHealthAggregates._load_earliest_snapshot_in_window
    )
    latest_snapshots = (
        ClanHealthAggregates._load_latest_snapshot_before_or_at
    )

    search_players = ClanHealthHistory._search_health_players
    latest_activity_season = (
        ClanHealthHistory._latest_activity_season_for_player
    )
    snapshot_history = ClanHealthHistory._load_snapshot_history
    player_war_attacks = ClanHealthHistory._load_player_war_attacks
    player_raid_activity = (
        ClanHealthHistory._load_player_raid_member_activity
    )
    player_trend_rows = ClanHealthHistory._load_player_trend_history
    player_movement = ClanHealthHistory._load_player_movement_segments
