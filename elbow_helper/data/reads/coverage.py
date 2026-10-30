"""Conservative coverage from receipts, compact player observation spans and war source status.

No gateway-access spans or collection_scopes exist in this record version.
Their absence is explicit: poll evidence never promises continuous tracking.
"""

import json

from ..store.wars import END_DRIFT
from .core import MAX_PAGE

SECOND = 1_000_000
CADENCES = {"clan_roster": 900 * SECOND, "account_links": 300 * SECOND,
            "discord_scan": 86400 * SECOND, "player_profile": 3600 * SECOND}


def signal(connection, kind, scope, at, *, after=None, before=None, account_id=None):
    cache = getattr(connection, "coverage_cache", {})
    cache_key = (kind, scope, at, after, before, account_id)
    if cache_key in cache:
        return cache[cache_key]
    cadence = CADENCES[kind]
    end = min(at, before) if before is not None else at
    args = [kind, end]
    where = "source_kind=? AND COALESCE(fetched_at,started_at)<=?"
    if scope is not None:
        where += " AND scope_id=?"
        args.append(scope)
    items = connection.execute(
        f"SELECT * FROM collection_items WHERE {where} ORDER BY COALESCE(fetched_at,started_at),id", args)
    points, failures, spans = [], [], []
    start, last_good, latest, population, complete_fields = None, None, None, None, {}

    def observe(at_point, good, status, count=None, fields=None):
        nonlocal last_good, latest, population, complete_fields
        latest = (at_point, status)
        if good:
            if last_good is not None and at_point > last_good + 2 * cadence:
                spans.append({"after": last_good + 2 * cadence, "before": at_point,
                              "cause": "missed_observation"})
            last_good = at_point
            points.append(at_point)
            population = count
            complete_fields = fields or {}
        else:
            failures.append({"at": at_point, "status": status})

    events = []
    for row in items:
        first = min(row["started_at"], row["fetched_at"]) if row["fetched_at"] is not None else row["started_at"]
        start = first if start is None else min(start, first)
        point = row["fetched_at"] or row["started_at"]
        if point > end:
            continue
        fields = json.loads(row["completeness_json"])
        good = row["status"] in {"ok", "empty"} and fields.get("complete", False)
        # Player failures are combined with compact successful spans below.
        if kind != "player_profile" or not good:
            events.append((point, good, row["error_code"] or row["status"], row["returned_count"], fields))
    if kind == "player_profile" and account_id is not None:
        for row in connection.execute("SELECT * FROM player_coverage WHERE account_id=? AND from_at<=? ORDER BY from_at",
                                      (account_id, end)):
            start = row["from_at"] if start is None else min(start, row["from_at"])
            fields = json.loads(row["completeness_json"])
            # A span retains endpoints, not every successful hourly point. Do
            # not fabricate an observation at an arbitrary historical time.
            status = "observed" if fields.get("complete", False) else "partial"
            events.append((row["from_at"], fields.get("fields", False), status, 1, fields))
            if row["from_at"] < row["last_observed_at"] <= end:
                events.append((row["last_observed_at"], fields.get("fields", False), status, 1, fields))
            if row["last_observed_at"] > row["from_at"]:
                spans.append({"after": row["from_at"], "before": min(end, row["last_observed_at"]),
                              "cause": "compact_observation_span"})
    for event in sorted(events, key=lambda value: value[0]):
        observe(*event)
    # Compact spans bridge retained endpoints, but cannot prove continuity.
    compact = [span for span in spans if span["cause"] == "compact_observation_span"]
    gaps = [span for span in spans if span["cause"] != "compact_observation_span"
            and not any(c["after"] <= span["after"] and c["before"] >= span["before"] for c in compact)]
    if last_good is not None and end > last_good + 2 * cadence:
        tail = last_good + 2 * cadence
        for span in compact:
            if span["after"] <= last_good <= span["before"]:
                tail = max(tail, span["before"] + 2 * cadence)
        if end > tail:
            gaps.append({"after": tail, "before": end, "cause": "overdue"})
    if after is not None and (start is None or after < start):
        gaps.insert(0, {"after": after, "before": min(end, start) if start is not None else end,
                        "cause": "not_tracked"})
    if after is not None:
        gaps = [gap for gap in gaps if gap["before"] > after]
        failures = [failure for failure in failures if failure["at"] >= after]
    tracked = start is not None and start <= end
    stale = bool(last_good is not None and end > last_good + 2 * cadence
                 and not any(s["after"] <= last_good and s["before"] + 2 * cadence >= end for s in compact))
    latest_ok = latest is not None and latest[1] in {"ok", "empty", "observed"}
    status = ("not_tracked" if not tracked else "unavailable" if last_good is None else
              "stale" if stale else "partial" if not latest_ok else "observed")
    result = {"signal": kind, "scope": scope, "requested": {"after": after, "before": before, "as_of": at},
            "collection_start": start, "last_good_at": last_good, "status": status,
            "expected_population": population, "source_complete": latest_ok and not stale,
            "field_completeness": complete_fields, "gaps": gaps[:MAX_PAGE],
            "failures": failures[-MAX_PAGE:], "observation_points": points[-MAX_PAGE:],
            "compact_spans": compact[-MAX_PAGE:], "cadence_us": cadence,
            "truncated": max(len(gaps), len(failures), len(points), len(compact)) > MAX_PAGE,
            "privacy_redacted": False, "continuous_state_known": False,
            "scope_basis": "observed_collection_items"}
    cache[cache_key] = result
    return result


# War sources keep a status per check instead of a receipt per success.
WAR_CADENCES = {"current_war": 300 * SECOND, "war_log": 86400 * SECOND,
                "cwl_group": 300 * SECOND, "cwl_war": 60 * SECOND}


def war_signal(connection, kind, scope, at):
    """Freshness, failed or private fetches and log limits of one war source."""
    cadence = WAR_CADENCES[kind]
    state = connection.execute("SELECT * FROM war_collection_state WHERE source_kind=? AND scope_id=?",
                               (kind, scope)).fetchone()
    failures = [{"at": row["fetched_at"] or row["started_at"], "status": row["error_code"] or row["status"],
                 "http_status": row["http_status"]} for row in connection.execute(
        "SELECT * FROM collection_items WHERE source_kind=? AND scope_id=? AND status='failed' "
        "AND COALESCE(fetched_at,started_at)<=? ORDER BY COALESCE(fetched_at,started_at) DESC,id DESC LIMIT ?",
        (kind, scope, at, MAX_PAGE + 1))][::-1]
    attempted = state is not None and state["last_attempt_at"] is not None
    last_good, outcome = (state["last_success_at"], state["last_outcome"]) if attempted else (None, None)
    # A source the collector stopped visiting stays due; a failing one moves on.
    overdue = attempted and bool(state["enabled"]) and at > state["next_due_at"] + cadence
    status = ("not_tracked" if not attempted else "private" if outcome == "private_log" else
              "unavailable" if last_good is None else "stale" if overdue else
              "partial" if outcome == "failed" else "observed")
    result = {"signal": kind, "scope": scope, "requested": {"after": None, "before": None, "as_of": at},
              "status": status, "last_attempt_at": state["last_attempt_at"] if attempted else None,
              "last_good_at": last_good, "last_outcome": outcome,
              "observed": json.loads(state["observed_json"]) if attempted else {},
              "enabled": bool(state["enabled"]) if state else False,
              "next_due_at": state["next_due_at"] if state else None, "source_complete": status == "observed",
              "gaps": [{"after": state["next_due_at"] + cadence, "before": at, "cause": "overdue"}] if overdue else [],
              "failures": failures[-MAX_PAGE:], "cadence_us": cadence, "truncated": len(failures) > MAX_PAGE,
              "privacy_redacted": False, "continuous_state_known": False, "scope_basis": "war_source_status"}
    if kind == "war_log":
        checkpoint = json.loads(state["checkpoint_json"]) if state else {}
        result["log_limits"] = {"history_complete": bool(checkpoint.get("history_complete")),
                                "api_history_limit": bool(checkpoint.get("api_history_limit")),
                                "backfill_pending": "after" in checkpoint}
    if kind in {"current_war", "war_log"}:
        conflicts = war_conflicts(connection, scope)
        result["identity_conflicts"] = conflicts[:MAX_PAGE]
        result["truncated"] |= len(conflicts) > MAX_PAGE
    return result


def war_conflicts(connection, clan_tag):
    """One clan's wars that share a clan pair and end, so a log entry cannot tell them apart.

    Ends within the log's drift count as shared. Newest first.
    """
    groups = []
    for row in connection.execute(
            "SELECT w.id,w.end_at,a.clan_tag AS first,b.clan_tag AS second FROM wars w "
            "JOIN war_sides a ON a.war_id=w.id AND a.side=0 JOIN war_sides b ON b.war_id=w.id AND b.side=1 "
            "WHERE ? IN (a.clan_tag,b.clan_tag) ORDER BY a.clan_tag,b.clan_tag,w.end_at", (clan_tag,)):
        pair = [row["first"], row["second"]]
        if groups and groups[-1]["clan_tags"] == pair and row["end_at"] - groups[-1]["last"] <= END_DRIFT:
            groups[-1]["war_ids"].append(row["id"])
            groups[-1]["last"] = row["end_at"]
        else:
            groups.append({"clan_tags": pair, "end_at": row["end_at"], "war_ids": [row["id"]], "last": row["end_at"]})
    return sorted(({"clan_tags": g["clan_tags"], "end_at": g["end_at"], "war_ids": sorted(g["war_ids"])}
                   for g in groups if len(g["war_ids"]) > 1), key=lambda g: g["end_at"], reverse=True)


def war_side_coverage(connection, war_id, clan_tag):
    """Lineup and attack completeness of a clan's side, kept apart from its opponent's.

    Returns None when the clan did not fight this war.
    """
    war = connection.execute("SELECT * FROM wars WHERE id=?", (war_id,)).fetchone()
    sides = {row["clan_tag"]: row for row in connection.execute("SELECT * FROM war_sides WHERE war_id=?", (war_id,))}
    if war is None or clan_tag not in sides:
        return None
    # A known defender places an attack on the other side; with neither known,
    # the attack could be either side's.
    unattributed = connection.execute(
        "SELECT COUNT(*) FROM war_attacks WHERE war_id=? AND attacker_member_id IS NULL AND defender_member_id IS NULL",
        (war_id,)).fetchone()[0]

    def side(row):
        members = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(name IS NULL OR townhall IS NULL OR source_map_position IS NULL),0) "
            "FROM war_members WHERE war_id=? AND side=?", (war_id, row["side"])).fetchone()
        stored = connection.execute(
            "SELECT COUNT(*) FROM war_attacks a JOIN war_members m ON m.id=a.attacker_member_id "
            "WHERE a.war_id=? AND m.side=? AND COALESCE(json_extract(a.extra_json,'$.stub'),0)=0",
            (war_id, row["side"])).fetchone()[0]
        lineup = war["team_size"] is not None and members[0] == war["team_size"] and not members[1]
        return {"clan_tag": row["clan_tag"], "team_size": war["team_size"], "members": members[0],
                "lineup_complete": lineup, "attacks_reported": row["attacks"], "attacks_stored": stored,
                "attacks_complete": lineup and not unattributed and row["attacks"] is not None and stored == row["attacks"]}

    other = next(row for tag, row in sides.items() if tag != clan_tag)
    return {"war_id": war_id, "type": war["type"], "type_basis": war["type_basis"], "state": war["state"],
            "final": war["final_at"] is not None, "own": side(sides[clan_tag]), "opponent": side(other),
            "unattributed_attacks": unattributed}


def envelope(signals, *, after=None, before=None, at=None, truncated=False):
    values = list(signals)
    return {"requested": {"after": after, "before": before, "as_of": at}, "signals": values,
            "complete": bool(values) and all(s["source_complete"] and not s["gaps"] for s in values),
            "truncated": truncated or any(s["truncated"] for s in values), "privacy_redacted": False,
            "first_observed_is_first_ever": False}
