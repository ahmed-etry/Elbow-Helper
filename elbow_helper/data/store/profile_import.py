"""Checked profile history writes inside the existing shared startup transaction."""

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256

from .legacy_links import ImportMismatch, digest
from .link_state import receipt
from .player_projection import COUNTERS
from .profile_import_plan import HEALTH, TOTALS, record_cutoffs, snapshot_profiles
from .projections import canonical_json


TOPIC = "profile_history"
COLLECTOR = "profile_import"


def row_digest(rows):
    hasher = sha256()
    for row in rows:
        hasher.update((canonical_json(list(row)) + "\n").encode())
    return hasher.hexdigest()


def verify_profiles(connection, frozen, checks, run, expected):
    for source, count in frozen.counts.items():
        if sum(frozen.outcomes.get(source, {}).values()) != count:
            raise ImportMismatch("source_rows_unaccounted:" + source)
    for table, (count, fingerprint) in expected.items():
        rows = connection.execute(f"SELECT * FROM {table} WHERE source_run_id=? ORDER BY 1,2,3", (run,)).fetchall()
        if len(rows) != count or row_digest(rows) != fingerprint:
            raise ImportMismatch("profile_readback_mismatch:" + table)
    for tag, cutoff in frozen.cutoffs.items():
        if cutoff is not None and any(stamp >= cutoff for stamp in frozen.events.get(tag, {})):
            raise ImportMismatch("profile_record_boundary")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise ImportMismatch("family_foreign_key_check")
    checks["verified"] = True


def write_profiles(connection, frozen):
    item = receipt(connection, COLLECTOR, TOPIC, frozen.frozen_at, sum(frozen.counts.values()))
    run = connection.execute("SELECT run_id FROM collection_items WHERE id=?", (item,)).fetchone()[0]
    accounts, created = {}, 0
    tags = sorted({tag for tag, _ in frozen.coverage})
    for tag in tags:
        row = connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()
        if row is None:
            identifier = connection.execute(
                "INSERT INTO accounts(player_tag,first_observed_at,tracking_status) VALUES (?,NULL,'legacy_profile')",
                (tag,)).lastrowid
            created += 1
        else:
            identifier = row[0]
        accounts[tag] = identifier
    planned = {table: [] for table in ("player_counters", "player_observations", "old_profile_totals",
                                      "profile_history_coverage")}
    field_changes, conflicting_fields = Counter(), Counter()
    for tag, times in sorted(frozen.events.items()):
        last = {}
        for stamp, samples in sorted(times.items()):
            counters, fields, totals, sources = {}, {}, {}, {}
            for origin, fast, slow, sums in samples:
                for key, value in fast.items():
                    if key in counters and value != counters[key]:
                        raise ImportMismatch("conflicting_profile_time")
                    counters[key] = value
                    sources[key] = origin
                fields.update(slow)
                totals.update(sums)
            if counters:
                conflicts = {key: value for key, value in counters.items() if key in COUNTERS and isinstance(value, list)}
                decreased = [key for key, value in counters.items() if key in COUNTERS and type(value) is int
                             and key in last and value < (min(last[key]) if isinstance(last[key], list) else last[key])]
                presence = {"fields": sum(1 << i for i, key in enumerate(COUNTERS) if key in counters),
                            "legendStatistics": "legendStatistics" in counters, "sources": sources}
                if conflicts:
                    presence["conflicts"] = conflicts
                    conflicting_fields.update(conflicts.keys())
                context = {"utc_month": datetime.fromtimestamp(stamp / 1_000_000, timezone.utc).strftime("%Y-%m")}
                if decreased:
                    context["decreased_fields"] = decreased
                uncertain = {key: last[key] for key in counters if key in COUNTERS and isinstance(last.get(key), list)}
                if uncertain:
                    context["previous_conflicts"] = uncertain
                planned["player_counters"].append((
                    accounts[tag], stamp, *(None if key in conflicts else counters.get(key) for key in COUNTERS),
                    canonical_json(counters["legendStatistics"]) if "legendStatistics" in counters else None,
                    "{}", "{}", canonical_json(presence), canonical_json(context), run))
                field_changes.update(key for key, value in counters.items() if key not in last or last[key] != value)
                last.update(counters)
            for key, value in sorted(fields.items()):
                planned["player_observations"].append((accounts[tag], key, stamp, canonical_json(value), None, run))
                field_changes[key] += 1
            if totals:
                planned["old_profile_totals"].append((accounts[tag], stamp, *(totals.get(key) for key in TOTALS), HEALTH, run))
    for (tag, origin), fact in sorted(frozen.coverage.items()):
        planned["profile_history_coverage"].append((
            accounts[tag], origin, *(fact[key] for key in (
                "first_attempt_at", "last_attempt_at", "first_good_at", "last_good_at", "samples",
                "partial_samples", "failed_samples")), canonical_json(fact["gaps"]), canonical_json(fact["failures"]),
            canonical_json(fact["fields"]), run))
    expected = {}
    for table, rows in planned.items():
        # Same sort as the read-back, including field-key order for observations.
        rows.sort(key=lambda row: row[:3])
        if rows:
            connection.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in rows[0])})", rows)
        expected[table] = (len(rows), row_digest(rows))
    checks = {"source_rows": frozen.counts, "outcomes": frozen.outcomes, "suppressed_by_ranked": frozen.suppressed,
              "created_accounts": created, "inserted": {table: len(rows) for table, rows in planned.items()},
              "conflicting_samples_by_field": dict(conflicting_fields),
              "field_changes": dict(field_changes), "coverage_basis": "old_source_samples_before_first_record_fact"}
    verify_profiles(connection, frozen, checks, run, expected)
    written = created + sum(len(rows) for rows in planned.values())
    connection.execute("UPDATE collection_runs SET rows_written=? WHERE id=?", (written, run))
    connection.execute("UPDATE collection_items SET written_count=? WHERE id=?", (written, item))
    connection.execute("INSERT INTO imports VALUES (?,?,?,?,?,?,?,?)", (
        TOPIC, frozen.version, frozen.digest, frozen.frozen_at, digest(accounts), frozen.frozen_at,
        canonical_json(checks), item))
    return {"imported": True, "checks": checks, "source_digest": frozen.digest, "frozen_at": frozen.frozen_at}


def snapshot_for_record(connection, health_path, ranked_path):
    return snapshot_profiles(health_path, ranked_path, record_cutoffs(connection))
