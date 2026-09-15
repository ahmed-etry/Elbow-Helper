"""Read-only summaries of clan-health evidence and coverage."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Mapping

from .seasons import _latest_completed_season_key
from .database import ClanHealthRepository

HEALTH_ATTACK_LIMIT = 40
HEALTH_RAID_LIMIT = 20
UTC = timezone.utc


def load_player_health(repository: ClanHealthRepository, player_tag: str, days: int) -> Mapping[str, Any]:
    cycle_end = datetime.now(UTC)
    cycle_start = cycle_end - timedelta(days=days)
    start_ts = int(cycle_start.timestamp())
    end_ts = int(cycle_end.timestamp())
    tags = {player_tag}

    directory = repository.search_players(player_tag, 5)
    season_key = _latest_completed_season_key(cycle_end)
    report = repository.completed_player_report(player_tag, end_ts)
    earliest = repository.earliest_snapshots(
        cycle_start_ts=start_ts,
        cycle_end_ts=end_ts,
        player_tags=tags,
    ).get(player_tag)
    latest = repository.latest_snapshots(
        cutoff_ts=end_ts,
        player_tags=tags,
    ).get(player_tag)
    recent_snapshots = repository.snapshot_history(player_tag, limit=2)
    war_activity = repository.war_activity(
        cycle_start_ts=start_ts,
        cycle_end_ts=end_ts,
        player_tags=tags,
    ).get(player_tag, {})
    raid_activity = repository.raid_activity(
        cycle_start_ts=start_ts,
        cycle_end_ts=end_ts,
        player_tags=tags,
    ).get(player_tag, {})
    donations = repository.snapshot_counters(
        cycle_start_ts=start_ts,
        cycle_end_ts=end_ts,
        player_tags=tags,
    ).get(player_tag, {})
    attacks = repository.player_war_attacks(
        player_tag=player_tag,
        cycle_start=cycle_start,
        cycle_end=cycle_end,
        limit=HEALTH_ATTACK_LIMIT + 1,
    )
    raids = repository.player_raid_activity(
        player_tag=player_tag,
        cycle_start=cycle_start,
        cycle_end=cycle_end,
        limit=HEALTH_RAID_LIMIT + 1,
    )
    movement = repository.player_movement(
        player_tag=player_tag,
        cycle_start=cycle_start,
        cycle_end=cycle_end,
        lookback_days=0,
    )
    trends = (
        repository.player_trend_rows(
            player_tag=player_tag,
            up_to_season_key=season_key,
            limit=6,
        )
        if season_key
        else []
    )
    return {
        "player_tag": player_tag,
        "window": {
            "days": days,
            "start": cycle_start.isoformat(),
            "end": cycle_end.isoformat(),
        },
        "directory_matches": directory,
        "latest_completed_report": health_report_summary(report),
        "earliest_progression_values_in_window": earliest,
        "latest_progression_values": latest,
        "recent_snapshots": recent_snapshots,
        "window_activity": {
            "war": war_activity,
            "raid": raid_activity,
            "donations": donations,
        },
        "recent_war_attacks": attacks[:HEALTH_ATTACK_LIMIT],
        "recent_raid_weekends": raids[:HEALTH_RAID_LIMIT],
        "coverage": {
            "war_attacks_truncated": len(attacks) > HEALTH_ATTACK_LIMIT,
            "raid_weekends_truncated": len(raids) > HEALTH_RAID_LIMIT,
            "note": "Activity totals cover the requested window; detail lists may be sampled. "
                    "Missing observations are not proof of inactivity. Completed reports cover "
                    "their own reporting periods, not the requested window. Check snapshot timestamps for freshness.",
        },
        "movement": movement,
        "completed_period_trends": [
            health_report_summary(row) for row in trends
        ],
    }


def load_clan_health(repository: ClanHealthRepository, clan_code: str) -> Mapping[str, Any]:
    now = datetime.now(UTC)
    run, rows = repository.latest_report_before(
        cycle_end_ts=int(now.timestamp()),
        selected_clans=[clan_code],
        completed_only=True,
    )
    severity = {
        "Needs Review": 0,
        "Watch": 1,
        "Insufficient data": 2,
        "Good": 3,
        "Healthy": 3,
        "Not tracked": 4,
    }
    summarized = [health_report_summary(row) for row in rows]
    summarized.sort(
        key=lambda row: (
            severity.get(str((row or {}).get("status") or ""), 5),
            str((row or {}).get("player_name") or "").casefold(),
        )
    )
    counts = Counter(str((row or {}).get("status") or "Unknown") for row in summarized)
    return {
        "clan_code": clan_code,
        "report_run": run,
        "status_counts": dict(counts),
        "players": summarized,
    }


def health_report_summary(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    flags = row.get("flags")
    if flags is None:
        try:
            flags = json.loads(str(row.get("flags_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            flags = []
    keys = (
        "season_key",
        "clan_code",
        "player_tag",
        "player_name",
        "status",
        "note",
        "war_hits_used",
        "war_hits_expected",
        "war_missed",
        "war_stars_total",
        "war_attack_count",
        "raid_attacks",
        "raid_expected",
        "raid_expected_estimated",
        "raid_loot",
        "donations",
        "donations_received",
        "townhall",
        "hero_sum",
        "games_total",
        "hero_delta",
        "capital_delta",
        "th_delta",
        "games_delta",
        "created_ts",
        "cycle_start_ts",
        "cycle_end_ts",
    )
    summary = {key: row.get(key) for key in keys if key in row}
    summary["flags"] = flags if isinstance(flags, list) else []
    return summary
