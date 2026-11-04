"""Compact watch facts, per account and watcher; no successful per-poll rows.

Legacy and record watchers have separate facts: overlapping windows must not
be counted twice, and a gap between watchers is not continuous coverage.
Failure spans bracket observations, not a claim that every battle was lost.
"""

import json

from .projections import canonical_json


COUNTS = ("polls", "clean_polls", "partial_polls", "failed_polls", "empty_polls", "nonempty_polls",
          "entries", "skipped_entries")
TIMES = ("first_attempt_at", "first_clean_at", "last_attempt_at", "last_clean_at")


def watch_fact():
    return {**dict.fromkeys(TIMES), **dict.fromkeys(COUNTS, 0), "failed_spans": []}


def observe(fact, at, status, entries=None, skipped=0, reason=None):
    """Fold one ordered poll into a fact; an empty clean log still starts watching."""
    if fact["last_attempt_at"] is not None and at < fact["last_attempt_at"]:
        raise ValueError("out_of_order_watch")
    if fact["first_attempt_at"] is None:
        fact["first_attempt_at"] = at
    fact["last_attempt_at"] = at
    fact["polls"] += 1
    fact["entries"] += entries or 0
    fact["skipped_entries"] += skipped
    if entries is not None:
        fact["empty_polls" if entries == 0 else "nonempty_polls"] += 1
    spans = fact["failed_spans"]
    if status == "ok":
        fact["clean_polls"] += 1
        if fact["first_clean_at"] is None:
            fact["first_clean_at"] = at
        fact["last_clean_at"] = at
        if spans and spans[-1]["next_clean_at"] is None:
            spans[-1]["next_clean_at"] = at
    else:
        fact["partial_polls" if status == "partial" else "failed_polls"] += 1
        if not spans or spans[-1]["next_clean_at"] is not None:
            spans.append({"from_at": at, "last_at": at, "previous_clean_at": fact["last_clean_at"],
                          "next_clean_at": None, "polls": 0, "skipped_entries": 0, "reasons": {}})
        span = spans[-1]
        span["last_at"] = at
        span["polls"] += 1
        span["skipped_entries"] += skipped
        reason = reason or status
        span["reasons"][reason] = span["reasons"].get(reason, 0) + 1


def save_watch(connection, tag, origin, fact, first_run, last_run):
    columns = ("player_tag", "origin", *TIMES, *COUNTS, "failed_spans_json", "first_run_id", "last_run_id")
    values = (tag, origin, *(fact[key] for key in (*TIMES, *COUNTS)),
              canonical_json(fact["failed_spans"]), first_run, last_run)
    connection.execute(
        f"INSERT INTO battle_log_coverage({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        f"ON CONFLICT(player_tag,origin) DO UPDATE SET {','.join(key+'=excluded.'+key for key in columns[2:])}", values)


def record_watch(connection, tag, run, at, status, *, entries=None, reason=None):
    old = connection.execute("SELECT * FROM battle_log_coverage WHERE player_tag=? AND origin='record'", (tag,)).fetchone()
    fact = ({**{key: old[key] for key in (*TIMES, *COUNTS)}, "failed_spans": json.loads(old["failed_spans_json"])}
            if old else watch_fact())
    observe(fact, at, "ok" if status in {"ok", "empty"} and reason is None else "failed", entries, reason=reason)
    save_watch(connection, tag, "record", fact, old["first_run_id"] if old else run, run)


def create_watch_schema(connection):
    connection.execute("""CREATE TABLE battle_log_coverage (
        player_tag TEXT NOT NULL, origin TEXT NOT NULL,
        first_attempt_at INTEGER NOT NULL, first_clean_at INTEGER,
        last_attempt_at INTEGER NOT NULL, last_clean_at INTEGER,
        polls INTEGER NOT NULL, clean_polls INTEGER NOT NULL, partial_polls INTEGER NOT NULL,
        failed_polls INTEGER NOT NULL, empty_polls INTEGER NOT NULL, nonempty_polls INTEGER NOT NULL,
        entries INTEGER NOT NULL, skipped_entries INTEGER NOT NULL, failed_spans_json TEXT NOT NULL,
        first_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        last_run_id INTEGER NOT NULL REFERENCES collection_runs(id), PRIMARY KEY(player_tag,origin))""")
    connection.execute("ALTER TABLE family_battles ADD COLUMN legacy_first_seen_json TEXT")
