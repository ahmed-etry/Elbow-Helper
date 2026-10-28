"""Lossless family logs and catalogues, with persistent request budgets.

At 279 accounts / nine clans: battle logs 6,696 GETs/day (hourly),
league history 279/day, ranked groups at most 1,116/day plus one per newly
discovered old reference, raids 216/day plus initial history pages, references
nine daily sweeps (one GET per page). Retries use the shared client's budget;
old ranked 500s use one attempt and become terminal unavailable observations.
"""

import json

from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction
from .api_fields import collect_fields
from .battles import identity as battle_key, save_battle
from .projections import canonical_json, nonnegative_int
from . import source_state
from .war_projection import api_time


SECOND = 1_000_000
DAY = 86400 * SECOND
# 31 captured logs: median about 5.5 and busiest about 12 battles/account/day. An hourly
# read has about 100 polls of headroom in the busiest observed 50-entry window.
# Ranked seasons last seven days (the API season ID is their Unix start time).
# Six-hour reads give four standings observations/day; finalize after the
# five-minute API cache window following the season boundary.
RANKED_INTERVAL = 6 * 3600 * SECOND
RANKED_WEEK = 7 * DAY
RANKED_FINAL_GRACE = 300 * SECOND
INTERVALS = {"battle_log": 3600 * SECOND, "league_history": DAY, "ranked_group": RANKED_INTERVAL,
             "capital_raids": 3600 * SECOND, "catalogue": DAY, "goldpass": DAY, "legend_seasons": DAY}
KINDS = {"battle_logs": ("battle_log",), "league_history": ("league_history",),
         "ranked_groups": ("ranked_group",), "capital_raids": ("capital_raids",),
         "reference_data": ("catalogue", "goldpass", "legend_seasons")}
REFERENCES = (("goldpass", "/goldpass/seasons/current"), ("legend_seasons", "/leagues/29000022/seasons"),
              *(("catalogue", "/" + path) for path in
                ("leagues", "leaguetiers", "warleagues", "builderbaseleagues", "capitalleagues", "labels/players", "labels/clans")))


def family_accounts(connection):
    return connection.execute(
        "SELECT a.id,a.player_tag FROM accounts a WHERE EXISTS ("
        "SELECT 1 FROM clan_membership m JOIN clans c ON c.id=m.clan_id "
        "WHERE m.account_id=a.id AND m.to_at IS NULL AND c.is_family=1) OR EXISTS ("
        "SELECT 1 FROM account_links l WHERE l.account_id=a.id AND l.to_at IS NULL) ORDER BY a.id").fetchall()


def ranked_references(connection, accounts, at):
    result = {}
    for account in accounts:
        rows = connection.execute("SELECT * FROM player_ranked_references WHERE account_id=? ORDER BY observed_at DESC",
                                  (account["id"],)).fetchall()
        for row in rows:
            for prefix in ("current", "previous"):
                pair = row[f"{prefix}_group_tag"], row[f"{prefix}_season_id"]
                if pair[0] and pair[1] is not None:
                    scope = canonical_json([account["player_tag"], *pair])
                    result[scope] = at < pair[1] * SECOND + RANKED_WEEK + RANKED_FINAL_GRACE
    return result


def due_sources(connection, collector, clans, at):
    """Discover scopes from saved membership/profiles, without network work."""
    with sqlite_transaction(connection, immediate=True):
        current = {}
        if collector in {"battle_logs", "league_history", "ranked_groups"}:
            accounts = family_accounts(connection)
            if collector == "ranked_groups":
                current = ranked_references(connection, accounts, at)
                specs = [("ranked_group", scope) for scope in current]
            else:
                specs = [(KINDS[collector][0], row["player_tag"]) for row in accounts]
        elif collector == "capital_raids":
            specs = [("capital_raids", clan.tag) for clan in clans]
        else:
            specs = list(REFERENCES)
        jobs = []
        for kind, scope in specs:
            connection.execute("INSERT OR IGNORE INTO family_api_state(source_kind,scope_id,next_due_at) VALUES (?,?,?)",
                               (kind, scope, at))
            row = connection.execute("SELECT * FROM family_api_state WHERE source_kind=? AND scope_id=?", (kind, scope)).fetchone()
            due = row["next_due_at"]
            if row["last_attempt_at"] is not None and kind in {"battle_log", "ranked_group"}:
                earliest = row["last_attempt_at"] + INTERVALS[kind]
                if kind == "ranked_group":
                    earliest = min(earliest, json.loads(scope)[2] * SECOND + RANKED_WEEK + RANKED_FINAL_GRACE)
                due = max(due, earliest)
            if row["enabled"] and due <= at and row["active_run_id"] is None:
                jobs.append({**dict(row), "current": current.get(scope, False)})
        return jobs


def entity_scope(scope, key):
    return canonical_json([scope, key])


def _entity(connection, kind, scope, key, raw, run, at, *, source_at=None, final=False):
    target = entity_scope(scope, key)
    previous = connection.execute(
        "SELECT 1 FROM family_api_entities WHERE source_kind=? AND scope_id=? AND entity_key=?", (kind, scope, key)).fetchone()
    connection.execute(
        "INSERT OR IGNORE INTO family_api_entities VALUES (?,?,?,?,?,?,?)",
        (kind, scope, key, target, source_at, at if final else None, run))
    if final:
        connection.execute("UPDATE family_api_entities SET final_at=? "
                           "WHERE source_kind=? AND scope_id=? AND entity_key=? AND final_at IS NULL", (at, kind, scope, key))
    collect_fields(connection, kind, target, raw, run, at)
    return previous is not None


def _items(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise ValueError("invalid_items")
    paging = raw.get("paging", {})
    if not isinstance(paging, dict) or not isinstance(paging.get("cursors", {}), dict):
        raise ValueError("invalid_paging")
    after = paging.get("cursors", {}).get("after")
    if after is not None and (not isinstance(after, str) or not after):
        raise ValueError("invalid_cursor")
    return raw["items"], after


def _save_list(connection, kind, scope, raw, checkpoint, run, at):
    items, after = _items(raw)
    if kind in {"battle_log", "league_history"} and after:
        raise ValueError("unexpected_pagination")
    if after and after == checkpoint.get("after"):
        raise ValueError("repeated_cursor")
    seen, overlap = set(), False
    for item in items:
        source_at, final = None, False
        if kind == "battle_log":
            key = battle_key(item)
            source_at, final = api_time(item["battleTimestamp"]), True
        elif kind == "league_history":
            if not isinstance(item, dict) or not nonnegative_int(item.get("leagueSeasonId")):
                raise ValueError("invalid_league_season")
            key, final = str(item["leagueSeasonId"]), True
            source_at = item["leagueSeasonId"] * SECOND
        elif kind == "capital_raids":
            if (not isinstance(item, dict) or api_time(item.get("startTime")) is None
                    or api_time(item.get("endTime")) is None or item.get("state") not in {"ongoing", "ended"}
                    or api_time(item["endTime"]) <= api_time(item["startTime"])):
                raise ValueError("invalid_raid_season")
            key, source_at = item["startTime"], api_time(item["startTime"])
            final = item["state"] == "ended"
        else:
            if not isinstance(item, dict) or type(item.get("id")) not in (str, int):
                raise ValueError("invalid_catalogue_identity")
            key = canonical_json(item["id"])
        if key in seen:
            raise ValueError("duplicate_source_identity")
        seen.add(key)
        overlap |= (save_battle(connection, scope, item, run, at) if kind == "battle_log" else
                    _entity(connection, kind, scope, key, item, run, at, source_at=source_at, final=final))
    # Keep all envelope fields, including empty lists and future metadata, but
    # never store a rolling window as a per-poll copy of its historical rows.
    collect_fields(connection, kind + "_envelope", scope, {**raw, "items": []}, run, at)
    gap = kind == "battle_log" and checkpoint.get("observed") and len(items) >= 50 and not overlap
    if kind == "capital_raids" and checkpoint.get("history_complete") and not checkpoint.get("after"):
        after = None  # Initial backfill is complete; the head keeps the live weekend.
    next_checkpoint = {"observed": True, "history_complete": not after,
                       **({"after": after} if after else {})}
    return len(items), next_checkpoint, bool(gap)


def record_source(connection, run, ordinal, job, response, at):
    """One endpoint transaction; validation failures roll back its facts only."""
    kind, scope = job["source_kind"], job["scope_id"]
    with sqlite_transaction(connection, immediate=True):
        state = connection.execute("SELECT * FROM family_api_state WHERE source_kind=? AND scope_id=? "
                                   "AND active_run_id=? AND ordinal=?", (kind, scope, run, ordinal)).fetchone()
        if state is None:
            raise ValueError("source_not_queued")
        checkpoint = json.loads(state["checkpoint_json"])
        due, enabled, count = at + INTERVALS[kind], state["enabled"], None
        if kind == "ranked_group" and job["current"]:
            final_due = json.loads(scope)[2] * SECOND + RANKED_WEEK + RANKED_FINAL_GRACE
            if at < final_due:
                due = min(due, final_due)
        status, error, written = "failed", "fetch_failed", 0
        if state["last_attempt_at"] is not None and at < state["last_attempt_at"]:
            raise ValueError("out_of_order_observation")
        connection.execute("SAVEPOINT source_facts")
        before = connection.total_changes
        try:
            if kind == "ranked_group" and not job["current"] and response and response.status == 500:
                enabled, error = 0, "old_season_unavailable"
            elif response and response.ok and response.error is None:
                raw = response.payload
                canonical_json(raw)
                if kind == "ranked_group":
                    if (not isinstance(raw, dict) or any(not isinstance(raw.get(key), list)
                                                       for key in ("members", "attackLogs", "defenseLogs"))):
                        raise ValueError("invalid_ranked_group")
                    collect_fields(connection, kind, scope, raw, run, at)
                    count, enabled = len(raw["members"]), int(job["current"])
                elif kind == "goldpass":
                    if (not isinstance(raw, dict) or api_time(raw.get("startTime")) is None
                            or api_time(raw.get("endTime")) is None or api_time(raw["endTime"]) <= api_time(raw["startTime"])):
                        raise ValueError("invalid_goldpass_season")
                    _entity(connection, kind, scope, raw["startTime"], raw, run, at, source_at=api_time(raw["startTime"]))
                    count = 1
                else:
                    count, checkpoint, gap = _save_list(connection, kind, scope, raw, checkpoint, run, at)
                    if gap:
                        error = "battle_window_gap"
                status = "ok" if count else "empty"
                if error == "battle_window_gap":
                    status = "failed"
                else:
                    error = None
                written = connection.total_changes - before
        except (ValueError, TypeError, OverflowError):
            connection.execute("ROLLBACK TO source_facts")
            checkpoint = json.loads(state["checkpoint_json"])
            status, error, written = "failed", "invalid_payload", 0
        connection.execute("RELEASE source_facts")
        connection.execute("UPDATE family_api_state SET next_due_at=?,enabled=?,checkpoint_json=? "
                           "WHERE source_kind=? AND scope_id=?", (due, enabled, canonical_json(checkpoint), kind, scope))
        source_state.outcome(connection, run, state, status, at, error=error, response=response, count=count, written=written)
        return checkpoint.get("after") if status in {"ok", "empty"} else None
