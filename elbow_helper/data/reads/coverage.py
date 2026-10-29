"""Conservative coverage from receipts and compact player observation spans.

No gateway-access spans or collection_scopes exist in this record version.
Their absence is explicit: poll evidence never promises continuous tracking.
"""

import json

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


def envelope(signals, *, after=None, before=None, at=None, truncated=False):
    values = list(signals)
    return {"requested": {"after": after, "before": before, "as_of": at}, "signals": values,
            "complete": bool(values) and all(s["source_complete"] and not s["gaps"] for s in values),
            "truncated": truncated or any(s["truncated"] for s in values), "privacy_redacted": False,
            "first_observed_is_first_ever": False}
