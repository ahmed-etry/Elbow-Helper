"""Bounded membership observations from complete family roster snapshots."""

import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3
import time

from elbow_helper.configuration.clans import CLANS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access, require_lookup_access
from ..capabilities.clan_health.data import SOURCES
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from ..research.history import search_date

RUN_LIMIT = 128
ROW_LIMIT = 100_000
GAP_SECONDS = 48 * 3600


def read_observations(path, history_after, before):
    deadline = time.monotonic() + 3
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        conn.execute("BEGIN")
        runs = [dict(row) for row in conn.execute(
            "SELECT run_id, created_ts FROM report_runs "
            "WHERE scope='BACKGROUND_ALL' AND partial=0 AND created_ts>=? AND created_ts<? "
            "ORDER BY created_ts DESC, run_id DESC LIMIT ?",
            (history_after, before, RUN_LIMIT + 1),
        )]
        clipped = len(runs) > RUN_LIMIT
        runs = runs[:RUN_LIMIT]
        rows = []
        if runs:
            marks = ",".join("?" for _ in runs)
            rows = [dict(row) for row in conn.execute(
                f"SELECT run_id, clan_code, player_tag, player_name FROM report_players "
                f"WHERE run_id IN ({marks}) LIMIT ?",
                (*[row["run_id"] for row in runs], ROW_LIMIT + 1),
            )]
            if len(rows) > ROW_LIMIT:
                raise ValueError("Membership snapshots exceed the row bound; narrow the window.")
        return list(reversed(runs)), rows, clipped


def membership_events(runs, rows, after, clan_codes):
    rosters = {run["run_id"]: {} for run in runs}
    for row in rows:
        roster = rosters[row["run_id"]]
        if row["player_tag"] in roster:
            raise ValueError("A family snapshot has conflicting memberships.")
        roster[row["player_tag"]] = row
    events, seen, previous = [], {}, {}
    previous_at = None
    for run in runs:
        current = rosters[run["run_id"]]
        for tag, row in current.items():
            old = previous.get(tag)
            if old is not None and old["clan_code"] == row["clan_code"]:
                continue
            if run["created_ts"] >= after and (not clan_codes or row["clan_code"] in clan_codes):
                kind = ("family_transfer" if old is not None else
                        "returning_observation" if tag in seen else "first_observed")
                events.append({
                    "player_tag": tag, "player_name": row["player_name"],
                    "clan_code": row["clan_code"], "kind": kind,
                    "observed_at": datetime.fromtimestamp(run["created_ts"], timezone.utc).isoformat(),
                    "previous_observation_at": (
                        datetime.fromtimestamp(previous_at, timezone.utc).isoformat()
                        if previous_at is not None else None),
                    "previous_clan_code": old["clan_code"] if old else None,
                    "prior_membership_observed_at": (
                        datetime.fromtimestamp(seen[tag], timezone.utc).isoformat() if tag in seen else None),
                    "arrival_time_known": False, "first_ever_membership_known": False,
                    "snapshot_gap": previous_at is None or run["created_ts"] - previous_at > GAP_SECONDS,
                })
        seen.update(dict.fromkeys(current, run["created_ts"]))
        previous, previous_at = current, run["created_ts"]
    return sorted(events, key=lambda row: (row["observed_at"], row["player_tag"]), reverse=True)


async def read_membership_history(context, arguments):
    await require_evidence_access(context)
    require_lookup_access(context, {"lead_plus"})
    try:
        after = search_date(arguments.get("after"))
        before = search_date(arguments.get("before")) or datetime.now(timezone.utc)
        history_after = search_date(arguments.get("history_after")) or (
            max(after - timedelta(days=30), before - timedelta(days=90)) if after else None)
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 25)
        codes = arguments.get("clan_codes", [])
        if (after is None or history_after is None or not history_after <= after < before
                or before - history_after > timedelta(days=90)
                or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 25
                or not isinstance(codes, list) or len(codes) > 20 or any(code not in CLANS for code in codes)):
            raise ValueError("Invalid membership selection")
    except (ValueError, TypeError, AttributeError):
        return {"error": "Use ISO dates within ninety days, valid clan codes and a page of 1..25."}
    path = next(source for source in SOURCES if source.alias == "health").resolve(context.bot.paths)
    try:
        runs, rows, clipped = await asyncio.to_thread(
            read_observations, path, history_after.timestamp(), before.timestamp(),
        )
        events = membership_events(runs, rows, after.timestamp(), codes)
    except (sqlite3.Error, OSError, ValueError):
        return {"error": "Membership observations could not be read within their bounds."}
    require_lookup_access(context, {"lead_plus"})
    await require_evidence_access(context)
    context.state.required_access.add("lead_plus")
    selected = events[offset:offset + limit]
    next_offset = offset + len(selected) if offset + len(selected) < len(events) else None
    times = [run["created_ts"] for run in runs]
    return {
        "source": "complete_family_health_snapshots", "events": selected,
        "matching_rows": len(events), "next_offset": next_offset, "truncated": clipped,
        "coverage": {
            "requested_after": after.isoformat(), "requested_before": before.isoformat(),
            "history_after": history_after.isoformat(), "before_exclusive": True,
            "observed_after": datetime.fromtimestamp(times[0], timezone.utc).isoformat() if times else None,
            "observed_before": datetime.fromtimestamp(times[-1], timezone.utc).isoformat() if times else None,
            "snapshot_count": len(runs), "snapshot_limit": RUN_LIMIT,
            "snapshot_limit_reached": clipped, "complete_snapshots_only": True,
            "max_snapshot_gap_seconds": max((b - a for a, b in zip(times, times[1:])), default=None),
            "history_complete": False, "first_ever_membership_known": False,
        },
        "limitations": {"observations_only": True, "arrivals_between_snapshots": True,
                        "earlier_membership_outside_window_unknown": True,
                        "partial_snapshots_excluded": True},
        "flags": {"status": "partial", "truncated": clipped, "limits": ["bounded_membership_observations"]},
    }


def membership_tools():
    return (RegisteredAgentTool(AgentToolDefinition(
        "read_membership_history", "Read bounded newcomer and returner observations from family snapshots.",
        {"type": "object", "properties": {
            "after": {"type": "string", "maxLength": 40},
            "before": {"type": "string", "maxLength": 40},
            "history_after": {"type": "string", "maxLength": 40},
            "clan_codes": {"type": "array", "maxItems": 20,
                           "items": {"type": "string", "enum": sorted(CLANS)}},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 25},
        }, "required": ["after"], "additionalProperties": False},
        usage_note="Max 90 days, 128 complete family snapshots. First observed is not first-ever membership; returns require earlier observations inside bounds.",
    ), read_membership_history, contract=CapabilityContract(required_access=frozenset({"lead_plus"}))),)
