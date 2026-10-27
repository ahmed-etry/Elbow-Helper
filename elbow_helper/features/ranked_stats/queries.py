"""Windowed ranked battle counts and player snapshots."""

from collections import defaultdict
from dataclasses import asdict, dataclass
import json


@dataclass(frozen=True)
class DailyBattles:
    date: str
    attacks_used: int
    defenses: int


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
    attacks_used: int | None
    attacks_available: int | None
    daily: list[DailyBattles]
    battle_records: int
    battle_log_polls: int
    failed_battle_log_polls: int
    skipped_battle_entries: int
    counts_complete: bool
    sample_count: int
    failed_samples: int
    first_observed_at: str
    last_observed_at: str


class RankedQueries:
    def __init__(self, store):
        self.store = store

    def cache_revision(self):
        from elbow_helper.infrastructure.persistence.revisions import sqlite_revision
        return sqlite_revision(self.store.path)

    def window(self, *, after, before, selector, values, offset=0, limit=25, include_battles=False):
        parameters = dict(after=after, before=before, selector=selector, values=values)
        rows, truncated = self.store.read_window(**parameters)
        battles, polls, battle_truncated = self.store.read_battle_window(**parameters)
        column = {"member_ids": "member_id", "account_tags": "player_tag", "clan_tags": "clan_tag"}[selector]
        snapshots_by_tag, battles_by_tag, polls_by_tag = defaultdict(list), defaultdict(list), defaultdict(list)
        matched = set()
        for source, grouped in ((rows, snapshots_by_tag), (battles, battles_by_tag), (polls, polls_by_tag)):
            for row in source:
                if row[column] in values:
                    grouped[row["player_tag"]].append(row)
                    matched.add(row[column])
        tags = set(snapshots_by_tag) | set(battles_by_tag) | set(polls_by_tag)
        accounts = [_account(tag, snapshots_by_tag[tag], battles_by_tag[tag], polls_by_tag[tag]) for tag in tags]
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
        return {
            "after": after, "before": before, "accounts": accounts[offset:offset + limit],
            "total_accounts": len(accounts), "offset": offset,
            "next_offset": offset + limit if offset + limit < len(accounts) else None,
            "unobserved_selections": [value for value in values if value not in matched],
            "complete": not (truncated or battle_truncated), "coverage_complete": False,
            **({"stored_battles": battles} if include_battles else {}),
            "limitations": ["selection_rank_uses_last_observed_trophies_within_selected_accounts",
                            "attacks_used_counts_stored_RANKED_and_LEGEND_attacks_including_losses",
                            "rolling_logs_and_collection_gaps_can_hide_battles", "attack_quota_unavailable",
                            "daily_counts_use_battle_timestamp_UTC_not_poll_time",
                            "battle_owner_and_clan_are_first_observed_associations"]
                            + (["snapshot_row_limit_reached"] if truncated else [])
                            + (["battle_row_limit_reached"] if battle_truncated else []),
        }


def _account(tag, rows, battles, polls):
    latest_good = next((row for row in reversed(rows) if row["status"] == "ok"), None)
    latest = rows[-1] if rows else (battles[-1] if battles else polls[-1])
    ranking = json.loads(latest_good["fields_json"]) if latest_good else {}
    daily = defaultdict(lambda: [0, 0])
    for battle in battles:
        bucket = daily[battle["battle_at"][:10]]
        bucket[0 if battle["attack"] else 1] += 1
    observed_times = [row["observed_at"] for row in rows + polls]
    observed_times.extend(row["first_observed_at"] for row in battles)
    observed_times.extend(row["last_observed_at"] for row in battles)
    known = bool(battles) or any(poll["status"] == "ok" for poll in polls)
    return asdict(RankedAccount(
        player_tag=tag, member_id=latest["member_id"],
        player_name=latest.get("player_name") or (latest_good["player_name"] if latest_good else None),
        clan_tag=latest["clan_tag"],
        status=("ok" if latest_good is latest else "stale") if latest_good else "unavailable",
        selection_rank=None, ranking=ranking,
        ranking_observed_at=latest_good["observed_at"] if latest_good else None,
        attacks_used=sum(battle["attack"] for battle in battles) if known else None,
        attacks_available=None,
        daily=[DailyBattles(day, attacks, defenses) for day, (attacks, defenses) in sorted(daily.items())],
        battle_records=len(battles), battle_log_polls=len(polls),
        failed_battle_log_polls=sum(poll["status"] != "ok" for poll in polls),
        skipped_battle_entries=sum(poll["skipped_entries"] for poll in polls), counts_complete=False,
        sample_count=len(rows), failed_samples=sum(row["status"] != "ok" for row in rows),
        first_observed_at=min(observed_times), last_observed_at=max(observed_times),
    ))
