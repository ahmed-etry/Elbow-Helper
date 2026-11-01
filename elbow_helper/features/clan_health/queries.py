"""Typed, read-only clan-health evidence for other features."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Mapping

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag

from .database import ClanHealthRepository


@dataclass(frozen=True, slots=True)
class ClanHealthReportRun:
    run_id: str
    created_ts: int
    season_key: str
    cycle_start_ts: int
    cycle_end_ts: int
    player_count: int


@dataclass(frozen=True, slots=True)
class ClanHealthPlayerRow:
    player_tag: str
    player_name: str
    clan_code: str
    status: str
    flags: tuple[str, ...]
    note: str
    war_hits_used: int
    war_hits_expected: int
    war_missed: int
    war_stars_total: float
    war_destruction_total: float
    war_attack_count: int
    raid_attacks: int
    raid_expected: int
    raid_expected_estimated: bool
    raid_loot: int
    donations: int
    donations_received: int
    townhall: int | None
    hero_sum: int | None
    games_total: int | None
    hero_delta: int | None
    capital_delta: int | None
    th_delta: int | None
    games_delta: int | None


@dataclass(frozen=True, slots=True)
class ClanHealthReportSnapshot:
    run: ClanHealthReportRun
    clan_code: str
    rows: tuple[ClanHealthPlayerRow, ...]
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompleteFamilySnapshotRun:
    run_id: str
    observed_ts: int
    row_count: int
    unique_account_count: int
    ambiguous_account_tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FamilySnapshotInterval:
    older_run_id: str
    older_observed_ts: int
    newer_run_id: str
    newer_observed_ts: int
    older_account_count: int
    newer_account_count: int
    movement_count: int
    excluded_ambiguous_account_tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FamilyAccountMovement:
    player_tag: str
    player_name: str
    transition: str
    from_clan_code: str | None
    to_clan_code: str | None
    older_run_id: str
    older_observed_ts: int
    newer_run_id: str
    newer_observed_ts: int


@dataclass(frozen=True, slots=True)
class FamilyMovementHistory:
    read_at: str
    runs: tuple[CompleteFamilySnapshotRun, ...]
    intervals: tuple[FamilySnapshotInterval, ...]
    movements: tuple[FamilyAccountMovement, ...]
    next_before_run_id: str | None


class ClanHealthQueries:
    """Stable feature-owned facade; callers never depend on repository SQL."""

    def __init__(self, repository: ClanHealthRepository):
        self._repository = repository

    async def search_players(
        self, query: str, *, limit: int = 10, offset: int = 0,
        clan_code: str | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        if clan_code is not None and clan_code not in CLANS:
            raise ValueError("Unknown Brown Elbow clan code")
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("Invalid player-directory search offset")
        rows = await asyncio.to_thread(
            self._repository.search_players, query, max(1, min(limit, 26)),
            offset=offset, clan_code=clan_code,
        )
        return tuple(rows)

    async def report_runs(
        self, clan_code: str, *, before_run_id: str | None = None,
        limit: int = 10,
    ) -> tuple[ClanHealthReportRun, ...]:
        if clan_code not in CLANS:
            raise ValueError("Unknown Brown Elbow clan code")
        if before_run_id is not None and (
            not isinstance(before_run_id, str) or not before_run_id
            or len(before_run_id) > 100
        ):
            raise ValueError("Invalid clan-health report cursor")
        rows = await asyncio.to_thread(
            self._repository.completed_report_runs,
            clan_code=clan_code, before_run_id=before_run_id,
            limit=max(1, min(limit, 26)),
        )
        return tuple(_report_run(row) for row in rows)

    async def report(
        self, clan_code: str, run_id: str,
    ) -> ClanHealthReportSnapshot | None:
        if clan_code not in CLANS:
            raise ValueError("Unknown Brown Elbow clan code")
        if not isinstance(run_id, str) or not run_id or len(run_id) > 100:
            raise ValueError("Invalid clan-health report identity")
        run, rows = await asyncio.to_thread(
            self._repository.completed_clan_report,
            run_id=run_id, clan_code=clan_code,
        )
        if run is None:
            return None
        issues = []
        parsed = []
        for row in rows:
            flags = _flags(row.get("flags_json"))
            if flags is None:
                flags = ()
                issues.append(f"invalid_flags:{row.get('player_tag', '')}")
            parsed.append(ClanHealthPlayerRow(
                player_tag=str(row.get("player_tag") or ""),
                player_name=str(row.get("player_name") or ""),
                clan_code=str(row.get("clan_code") or ""),
                status=str(row.get("status") or ""),
                flags=flags,
                note=str(row.get("note") or ""),
                war_hits_used=int(row.get("war_hits_used") or 0),
                war_hits_expected=int(row.get("war_hits_expected") or 0),
                war_missed=int(row.get("war_missed") or 0),
                war_stars_total=float(row.get("war_stars_total") or 0.0),
                war_destruction_total=float(row.get("war_destruction_total") or 0.0),
                war_attack_count=int(row.get("war_attack_count") or 0),
                raid_attacks=int(row.get("raid_attacks") or 0),
                raid_expected=int(row.get("raid_expected") or 0),
                raid_expected_estimated=bool(row.get("raid_expected_estimated")),
                raid_loot=int(row.get("raid_loot") or 0),
                donations=int(row.get("donations") or 0),
                donations_received=int(row.get("donations_received") or 0),
                townhall=_optional_int(row.get("townhall")),
                hero_sum=_optional_int(row.get("hero_sum")),
                games_total=_optional_int(row.get("games_total")),
                hero_delta=_optional_int(row.get("hero_delta")),
                capital_delta=_optional_int(row.get("capital_delta")),
                th_delta=_optional_int(row.get("th_delta")),
                games_delta=_optional_int(row.get("games_delta")),
            ))
        snapshot = ClanHealthReportSnapshot(
            run=_report_run({**run, "player_count": len(parsed)}),
            clan_code=clan_code, rows=tuple(parsed), issues=tuple(issues),
        )
        _validate_snapshot(snapshot)
        return snapshot

    async def latest_report(self, clan_code: str) -> ClanHealthReportSnapshot | None:
        runs = await self.report_runs(clan_code, limit=1)
        if not runs:
            return None
        return await self.report(clan_code, runs[0].run_id)

    async def family_movement_history(
        self, *, interval_limit: int = 10,
        before_run_id: str | None = None,
    ) -> FamilyMovementHistory:
        if type(interval_limit) is not int or not 1 <= interval_limit <= 20:
            raise ValueError("Invalid family-movement interval limit")
        if before_run_id is not None and (
            not isinstance(before_run_id, str) or not before_run_id
            or len(before_run_id) > 100
        ):
            raise ValueError("Invalid family-snapshot cursor")
        data = await asyncio.to_thread(
            self._repository.complete_family_snapshots,
            run_limit=interval_limit + 2, before_run_id=before_run_id,
        )
        raw_runs = data["runs"]
        has_more = len(raw_runs) > interval_limit + 1
        selected_runs = raw_runs[:interval_limit + 1]
        selected_ids = {str(row.get("run_id") or "") for row in selected_runs}
        rows_by_run: dict[str, list[Mapping[str, Any]]] = {
            run_id: [] for run_id in selected_ids
        }
        for row in data["rows"]:
            run_id = str(row.get("run_id") or "")
            if run_id in selected_ids:
                rows_by_run[run_id].append(row)

        runs = []
        accounts_by_run: dict[str, dict[str, tuple[str, str]]] = {}
        for raw_run in selected_runs:
            run_id = _required_text(raw_run.get("run_id"), maximum=100)
            observed_ts = _required_int(raw_run.get("created_ts"), minimum=1)
            raw_rows = rows_by_run.get(run_id, [])
            if _required_int(raw_run.get("row_count"), minimum=0) != len(raw_rows):
                raise ValueError("Invalid complete family-snapshot coverage")
            grouped: dict[str, list[tuple[str, str]]] = {}
            for row in raw_rows:
                tag = normalize_player_tag(row.get("player_tag"))
                if tag is None:
                    raise ValueError("Invalid account in complete family snapshot")
                clan_code = _required_text(row.get("clan_code"), maximum=20)
                if clan_code not in CLANS:
                    raise ValueError("Unknown clan in complete family snapshot")
                name = _required_text(row.get("player_name"), maximum=100)
                grouped.setdefault(tag, []).append((clan_code, name))
            ambiguous = tuple(sorted(
                tag for tag, observations in grouped.items()
                if len(observations) != 1
            ))
            accounts_by_run[run_id] = {
                tag: observations[0] for tag, observations in grouped.items()
                if len(observations) == 1
            }
            runs.append(CompleteFamilySnapshotRun(
                run_id=run_id, observed_ts=observed_ts,
                row_count=len(raw_rows), unique_account_count=len(grouped),
                ambiguous_account_tags=ambiguous,
            ))

        intervals = []
        movements = []
        for index in range(max(0, len(runs) - 1)):
            newer, older = runs[index], runs[index + 1]
            if newer.observed_ts < older.observed_ts:
                raise ValueError("Complete family snapshots are not ordered newest first")
            ambiguous = tuple(sorted(set(
                newer.ambiguous_account_tags + older.ambiguous_account_tags
            )))
            older_accounts = accounts_by_run[older.run_id]
            newer_accounts = accounts_by_run[newer.run_id]
            interval_movements = []
            for tag in sorted((set(older_accounts) | set(newer_accounts)) - set(ambiguous)):
                old = older_accounts.get(tag)
                new = newer_accounts.get(tag)
                if old is not None and new is not None and old[0] == new[0]:
                    continue
                if old is None:
                    transition, from_clan, to_clan = "observed_entered_family", None, new[0]
                elif new is None:
                    transition, from_clan, to_clan = "observed_left_family", old[0], None
                else:
                    transition, from_clan, to_clan = "observed_family_clan_change", old[0], new[0]
                evidence = FamilyAccountMovement(
                    player_tag=tag, player_name=(new or old)[1],
                    transition=transition,
                    from_clan_code=from_clan, to_clan_code=to_clan,
                    older_run_id=older.run_id, older_observed_ts=older.observed_ts,
                    newer_run_id=newer.run_id, newer_observed_ts=newer.observed_ts,
                )
                interval_movements.append(evidence)
            interval_movements.sort(key=lambda row: (
                row.transition, row.player_name.casefold(), row.player_tag,
            ))
            movements.extend(interval_movements)
            intervals.append(FamilySnapshotInterval(
                older_run_id=older.run_id, older_observed_ts=older.observed_ts,
                newer_run_id=newer.run_id, newer_observed_ts=newer.observed_ts,
                older_account_count=older.unique_account_count,
                newer_account_count=newer.unique_account_count,
                movement_count=len(interval_movements),
                excluded_ambiguous_account_tags=ambiguous,
            ))
        return FamilyMovementHistory(
            read_at=datetime.now(timezone.utc).isoformat(), runs=tuple(runs),
            intervals=tuple(intervals), movements=tuple(movements),
            next_before_run_id=(runs[-1].run_id if has_more and runs else None),
        )


def _report_run(row: Mapping[str, Any]) -> ClanHealthReportRun:
    run = ClanHealthReportRun(
        run_id=str(row.get("run_id") or ""),
        created_ts=int(row.get("created_ts") or 0),
        season_key=str(row.get("season_key") or ""),
        cycle_start_ts=int(row.get("cycle_start_ts") or 0),
        cycle_end_ts=int(row.get("cycle_end_ts") or 0),
        player_count=int(row.get("player_count") or 0),
    )
    if (
        not run.run_id or len(run.run_id) > 100 or not run.season_key
        or run.created_ts <= 0 or run.cycle_start_ts <= 0
        or run.cycle_end_ts <= run.cycle_start_ts or run.player_count < 0
    ):
        raise ValueError("Invalid stored clan-health report identity")
    return run


def _flags(value: Any) -> tuple[str, ...] | None:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        return None
    return tuple(parsed)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _required_int(
    value: Any, *, minimum: int, maximum: int | None = None,
) -> int:
    if type(value) is not int or value < minimum or (
        maximum is not None and value > maximum
    ):
        raise ValueError("Invalid stored regular-war integer")
    return value


def _required_text(value: Any, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("Invalid stored regular-war text")
    return value


def _validate_snapshot(snapshot: ClanHealthReportSnapshot) -> None:
    if snapshot.clan_code not in CLANS or snapshot.run.player_count != len(snapshot.rows):
        raise ValueError("Invalid stored clan-health report coverage")
    tags = [row.player_tag for row in snapshot.rows]
    if len(tags) != len(set(tags)):
        raise ValueError("Duplicate account in stored clan-health report")
    for row in snapshot.rows:
        if (
            normalize_player_tag(row.player_tag) != row.player_tag
            or row.clan_code != snapshot.clan_code
            or not row.player_name or not row.status
            or any(not isinstance(flag, str) or not flag for flag in row.flags)
        ):
            raise ValueError("Invalid stored clan-health player row")


__all__ = [
    "ClanHealthPlayerRow", "ClanHealthQueries", "ClanHealthReportRun",
    "ClanHealthReportSnapshot", "CompleteFamilySnapshotRun",
    "FamilyAccountMovement", "FamilyMovementHistory", "FamilySnapshotInterval",
]
