"""Windowed ranked observations without inventing attack counts or quotas."""

from dataclasses import asdict, dataclass
from datetime import datetime
from itertools import groupby
import json


@dataclass(frozen=True)
class DailyWins:
    date: str
    attack_wins_observed: int | None
    observed_intervals: int


@dataclass(frozen=True)
class RankedAccount:
    player_tag: str
    member_id: int
    player_name: str | None
    clan_tag: str | None
    status: str
    selection_rank: int | None
    ranking: dict
    ranking_observed_at: str | None
    attack_wins_observed: int | None
    attacks_used: int | None
    attacks_available: int | None
    daily: list[DailyWins]
    sample_count: int
    failed_samples: int
    counter_resets: int
    max_gap_seconds: float | None
    first_observed_at: str
    last_observed_at: str


class RankedQueries:
    def __init__(self, store):
        self.store = store

    def window(self, *, after, before, selector, values, offset=0, limit=25):
        rows, truncated = self.store.read_window(after=after, before=before, selector=selector, values=values)
        column = {"member_ids": "member_id", "account_tags": "player_tag", "clan_tags": "clan_tag"}[selector]
        accounts = []
        for tag, group in groupby(rows, key=lambda row: row["player_tag"]):
            observations = list(group)
            if any(row[column] in values for row in observations):
                accounts.append(_account(tag, observations, selector, values))
        accounts.sort(key=lambda row: (row["ranking"].get("trophies") is None,
                                      -row["ranking"].get("trophies", 0), row["player_tag"]))
        last_trophies, rank = None, None
        for index, row in enumerate(accounts):
            trophies = row["ranking"].get("trophies")
            if trophies is not None:
                if trophies != last_trophies:
                    rank = index + 1
                row["selection_rank"] = rank
                last_trophies = trophies
        matched = set()
        matched.update(row[column] for row in rows)
        return {
            "after": after, "before": before, "accounts": accounts[offset:offset + limit],
            "total_accounts": len(accounts), "offset": offset,
            "next_offset": offset + limit if offset + limit < len(accounts) else None,
            "unobserved_selections": [value for value in values if value not in matched],
            "complete": not truncated, "coverage_complete": False,
            "limitations": ["selection_rank_uses_last_observed_trophies_within_selected_accounts",
                            "attackWins_is_wins_not_ranked_attacks_used", "attack_quota_unavailable",
                            "daily_wins_cover_only_observed_intervals_within_one_UTC_day",
                            "window_edges_and_counter_resets_can_hide_wins"]
                            + (["snapshot_row_limit_reached"] if truncated else []),
        }


def _account(tag, rows, selector, values):
    column = {"member_ids": "member_id", "account_tags": "player_tag", "clan_tags": "clan_tag"}[selector]
    scoped = [row for row in rows if row[column] in values]
    latest_good = next((row for row in reversed(scoped) if row["status"] == "ok"), None)
    latest = scoped[-1]
    ranking = json.loads(latest_good["fields_json"]) if latest_good else {}
    daily = {}
    wins, intervals, resets = 0, 0, 0
    max_gap = None
    previous = None
    for row in rows:
        if row[column] not in values:
            previous = None
            continue
        observed = datetime.fromisoformat(row["observed_at"])
        day = observed.date().isoformat()
        bucket = daily.setdefault(day, [0, 0])
        fields = json.loads(row["fields_json"])
        count = fields.get("attackWins")
        if row["status"] != "ok" or type(count) is not int:
            previous = None
            continue
        if previous is not None:
            old_row, old_time, old_count, old_fields = previous
            gap = (observed - old_time).total_seconds()
            max_gap = gap if max_gap is None else max(max_gap, gap)
            old_season = old_fields.get("legendStatistics", {}).get("currentSeason", {}).get("id")
            season = fields.get("legendStatistics", {}).get("currentSeason", {}).get("id")
            reset = count < old_count or (old_season is not None and season is not None and season != old_season)
            same_owner = old_row["member_id"] == row["member_id"]
            same_clan = old_row["clan_tag"] == row["clan_tag"]
            if reset:
                resets += 1
            elif same_owner and same_clan:
                wins += count - old_count
                intervals += 1
                if old_time.date() == observed.date():
                    bucket[0] += count - old_count
                    bucket[1] += 1
        previous = (row, observed, count, fields)
    return asdict(RankedAccount(
        player_tag=tag, member_id=latest["member_id"],
        player_name=latest["player_name"] or (latest_good["player_name"] if latest_good else None),
        clan_tag=latest["clan_tag"],
        status=("ok" if latest_good is latest else "stale") if latest_good else "unavailable",
        selection_rank=None, ranking=ranking,
        ranking_observed_at=latest_good["observed_at"] if latest_good else None,
        attack_wins_observed=wins if intervals else None,
        attacks_used=None, attacks_available=None,
        daily=[DailyWins(day, total if samples else None, samples)
               for day, (total, samples) in sorted(daily.items())],
        sample_count=len(scoped), failed_samples=sum(row["status"] != "ok" for row in scoped),
        counter_resets=resets, max_gap_seconds=max_gap,
        first_observed_at=scoped[0]["observed_at"], last_observed_at=scoped[-1]["observed_at"],
    ))
