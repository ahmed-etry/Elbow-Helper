"""Checked ranked battles and compact watch coverage for the shared startup copy.

Profiles are counted with a deferral reason, never copied. The legacy database
is read in one read-only transaction and is never opened by its feature store.
"""

from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from . import now_us
from .api_fields import leaves
from .battle_coverage import observe, save_watch, watch_fact
from .battles import _find, identity, read_battle_fields, save_battle
from .legacy_links import ImportMismatch, digest
from .link_state import receipt
from .projections import canonical_json, nonnegative_int
from .war_projection import api_time


TOPIC = "ranked_battles"
COLLECTOR = "ranked_import"
ORIGIN = "legacy_ranked"
TABLES = {
    "snapshots": ("player_tag", "observed_at", "member_id", "clan_tag", "player_name", "status", "fields_json"),
    "ranked_battles": ("battle_key", "player_tag", "battle_at", "battle_type", "attack", "opponent_tag", "member_id",
                       "clan_tag", "player_name", "first_observed_at", "last_observed_at", "payload_json"),
    "battle_log_polls": ("player_tag", "observed_at", "member_id", "clan_tag", "status", "total_entries",
                         "oldest_battle_at", "newest_battle_at", "skipped_entries", "poll_interval_seconds"),
    "profile_health": ("player_tag", "missing"),
}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def bad(table):
    raise ImportMismatch("bad_row:" + table)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("bad_timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive_timestamp")
    delta = parsed.astimezone(timezone.utc) - EPOCH
    result = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    if result < 0:
        raise ValueError("bad_timestamp")
    return result


@dataclass(frozen=True)
class RankedSnapshot:
    version: int
    digest: str
    frozen_at: int
    battles: list
    coverage: dict
    owners: dict
    counts: dict
    excluded: dict


def snapshot_ranked(path, at=None):
    frozen_at = now_us() if at is None else at
    if not nonnegative_int(frozen_at):
        raise ValueError("invalid_freeze_time")
    battles, coverage, owners = [], {}, {}
    counts, excluded = Counter(), defaultdict(Counter)
    hasher = sha256()
    seen = set()
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
            source.row_factory = sqlite3.Row
            source.execute("PRAGMA query_only=ON")
            with sqlite_transaction(source):
                version = source.execute("PRAGMA user_version").fetchone()[0]
                if version != 1:
                    raise ImportMismatch("unsupported_ranked_version")
                hasher.update(canonical_json(version).encode("utf-8"))
                for table, columns in TABLES.items():
                    counts[table] = 0
                    found = {row[1] for row in source.execute(f"PRAGMA table_info({table})")}
                    if found != set(columns):
                        raise ImportMismatch("legacy_schema_mismatch:" + table)
                    order = "player_tag" if table == "profile_health" else (
                        "player_tag,battle_at,battle_key" if table == "ranked_battles" else "player_tag,observed_at")
                    hasher.update(table.encode("utf-8"))
                    for row in source.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}"):
                        row = dict(row)
                        hasher.update((canonical_json(row) + "\n").encode("utf-8"))
                        counts[table] += 1
                        tag = row["player_tag"]
                        if normalize_player_tag(tag) != tag or tag is None:
                            bad(table)
                        if table == "profile_health":
                            if row["missing"] not in (0, 1):
                                bad(table)
                            excluded[tag][table] += 1
                            continue
                        owner = row["member_id"]
                        if not nonnegative_int(owner) or owner == 0 or owners.setdefault(tag, owner) != owner:
                            bad(table)
                        clan = row["clan_tag"]
                        if clan is not None and normalize_player_tag(clan) != clan:
                            bad(table)
                        if table == "ranked_battles":
                            raw = json.loads(row["payload_json"])
                            key = identity(raw)
                            time, opponent, attack = json.loads(key)
                            battle_at, first, last = (timestamp(row[name]) for name in (
                                "battle_at", "first_observed_at", "last_observed_at"))
                            if (battle_at != api_time(time) or not battle_at <= first <= last <= frozen_at
                                    or raw.get("battleType") not in {"ranked", "legend"}
                                    or row["battle_type"] != raw["battleType"].upper()
                                    or row["attack"] not in (0, 1) or row["attack"] != attack
                                    or row["opponent_tag"] != normalize_player_tag(raw.get("opponentPlayerTag"))
                                    or not isinstance(row["battle_key"], str) or not row["battle_key"]):
                                bad(table)
                            if (tag, battle_at, opponent, attack) in seen:
                                raise ImportMismatch("duplicate_ranked_identity")
                            seen.add((tag, battle_at, opponent, attack))
                            battles.append({"tag": tag, "raw": raw, "key": key, "battle_at": battle_at,
                                            "provenance": {"clan_when_first_seen": clan, "observed_at": first,
                                                           "basis": "clan_when_first_seen", "source": ORIGIN}})
                            continue
                        observed = timestamp(row["observed_at"])
                        if observed > frozen_at:
                            bad(table)
                        if table == "snapshots":
                            if row["status"] not in {"ok", "unavailable", "not_found"}:
                                bad(table)
                            excluded[tag][table] += 1
                            continue
                        status, entries, skipped = (row[key] for key in ("status", "total_entries", "skipped_entries"))
                        if (status not in {"ok", "partial", "unavailable"} or not nonnegative_int(skipped)
                                or not isinstance(row["poll_interval_seconds"], (int, float))
                                or not math.isfinite(row["poll_interval_seconds"]) or row["poll_interval_seconds"] <= 0):
                            bad(table)
                        if status == "unavailable":
                            if entries is not None or skipped or row["oldest_battle_at"] or row["newest_battle_at"]:
                                bad(table)
                        elif (not nonnegative_int(entries) or skipped > entries
                              or (status == "ok" and skipped) or (status == "partial" and not skipped)):
                            bad(table)
                        oldest, newest = row["oldest_battle_at"], row["newest_battle_at"]
                        if (oldest is None) != (newest is None) or (
                                oldest is not None and not timestamp(oldest) <= timestamp(newest) <= observed):
                            bad(table)
                        observe(coverage.setdefault(tag, watch_fact()), observed, status, entries, skipped)
    except sqlite3.Error as error:
        raise ImportMismatch("ranked_unreadable") from error
    except (ValueError, TypeError, OverflowError) as error:
        raise ImportMismatch("bad_ranked_row") from error
    if set(coverage) != set(owners) or not set(excluded) <= set(owners):
        raise ImportMismatch("ranked_account_set_mismatch")
    return RankedSnapshot(version, hasher.hexdigest(), frozen_at, battles, coverage, owners, dict(counts),
                          {tag: dict(rows) for tag, rows in excluded.items()})


def plan_ranked(connection, frozen):
    """Validate every account and match every battle before writing any import facts."""
    accounts = {}
    for tag, owner in frozen.owners.items():
        rows = connection.execute(
            "SELECT a.id,m.discord_id FROM accounts a LEFT JOIN account_links l ON l.account_id=a.id AND l.to_at IS NULL "
            "LEFT JOIN members m ON m.id=l.member_id WHERE a.player_tag=?", (tag,)).fetchall()
        if len(rows) != 1 or rows[0][1] != owner:
            raise ImportMismatch("ranked_account_owner_mismatch")
        accounts[tag] = rows[0][0]
    decisions = []
    for battle in frozen.battles:
        stamp, opponent, attack = json.loads(battle["key"])
        old = _find(connection, battle["tag"], stamp, opponent, attack)
        if old is not None:
            fields = read_battle_fields(connection, canonical_json([battle["tag"], battle["key"]]))
            expected = {path: value for path, value, _ in leaves(battle["raw"])}
            if canonical_json(fields) != canonical_json(expected):
                raise ImportMismatch("ranked_battle_conflict")
            if old["legacy_first_seen_json"] is not None and old["legacy_first_seen_json"] != canonical_json(battle["provenance"]):
                raise ImportMismatch("ranked_clan_provenance_conflict")
        decisions.append(old["id"] if old else None)
    return accounts, decisions


def verify_ranked(connection, frozen, checks):
    for table, count in frozen.counts.items():
        if sum(checks["outcomes"].get(table, {}).values()) != count:
            raise ImportMismatch("source_rows_unaccounted:" + table)
    for battle in frozen.battles:
        stamp, opponent, attack = json.loads(battle["key"])
        stored = _find(connection, battle["tag"], stamp, opponent, attack)
        fields = read_battle_fields(connection, canonical_json([battle["tag"], battle["key"]]))
        if (stored is None or stored["legacy_first_seen_json"] != canonical_json(battle["provenance"])
                or canonical_json(fields) != canonical_json({p: v for p, v, _ in leaves(battle["raw"])})):
            raise ImportMismatch("ranked_battle_mismatch")
    for tag, fact in frozen.coverage.items():
        stored = connection.execute("SELECT * FROM battle_log_coverage WHERE player_tag=? AND origin=?", (tag, ORIGIN)).fetchone()
        if stored is None or any(stored[key] != value for key, value in fact.items() if key != "failed_spans") or (
                json.loads(stored["failed_spans_json"]) != fact["failed_spans"]):
            raise ImportMismatch("ranked_watch_mismatch")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise ImportMismatch("family_foreign_key_check")


def write_ranked(connection, frozen, plan):
    """Called only inside the shared startup transaction; marker and facts commit together."""
    accounts, decisions = plan
    item = receipt(connection, COLLECTOR, TOPIC, frozen.frozen_at, len(frozen.battles))
    run = connection.execute("SELECT run_id FROM collection_items WHERE id=?", (item,)).fetchone()[0]
    per_account = {tag: {"account_id": accounts[tag], "battles": 0, "matched": 0, "added": 0, "before_first_poll": 0,
                         "excluded": frozen.excluded.get(tag, {}), "polls": fact["polls"]}
                   for tag, fact in frozen.coverage.items()}
    for battle, existing in zip(frozen.battles, decisions):
        tag = battle["tag"]
        if existing is None:
            save_battle(connection, tag, battle["raw"], run, frozen.frozen_at)
            stamp, opponent, attack = json.loads(battle["key"])
            stored = _find(connection, tag, stamp, opponent, attack)
            if stored is None:
                raise ImportMismatch("ranked_battle_mismatch")
            existing = stored["id"]
            outcome = "added"
        else:
            outcome = "matched"
        connection.execute("UPDATE family_battles SET legacy_first_seen_json=? WHERE id=? AND legacy_first_seen_json IS NULL",
                           (canonical_json(battle["provenance"]), existing))
        per_account[tag]["battles"] += 1
        per_account[tag][outcome] += 1
        per_account[tag]["before_first_poll"] += battle["battle_at"] < frozen.coverage[tag]["first_attempt_at"]
    for tag, fact in frozen.coverage.items():
        save_watch(connection, tag, ORIGIN, fact, run, run)
    matched = sum(row["matched"] for row in per_account.values())
    added = sum(row["added"] for row in per_account.values())
    checks = {"source_rows": frozen.counts, "outcomes": {
        "ranked_battles": {"matched": matched, "imported": added},
        "battle_log_polls": {"compacted": sum(fact["polls"] for fact in frozen.coverage.values())},
        "snapshots": {"excluded:profile_history_deferred": sum(r.get("snapshots", 0) for r in frozen.excluded.values())},
        "profile_health": {"excluded:operational_state": sum(r.get("profile_health", 0) for r in frozen.excluded.values())}},
        "accounts": per_account, "inserted": {"battles": added, "watch_facts": len(frozen.coverage)},
        "before_first_poll": sum(row["before_first_poll"] for row in per_account.values())}
    verify_ranked(connection, frozen, checks)
    written = added + len(frozen.coverage)
    connection.execute("UPDATE collection_runs SET rows_written=? WHERE id=?", (written, run))
    connection.execute("UPDATE collection_items SET written_count=? WHERE id=?", (written, item))
    connection.execute("INSERT INTO imports VALUES (?,?,?,?,?,?,?,?)",
                       (TOPIC, frozen.version, frozen.digest, frozen.frozen_at, digest(accounts), frozen.frozen_at,
                        canonical_json(checks), item))
    return {"imported": True, "source_version": frozen.version, "source_digest": frozen.digest,
            "frozen_at": frozen.frozen_at, "checks": checks}
