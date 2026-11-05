"""All-or-nothing copy of old clan health history the record lacks: raid lines, clan stays and names.

Read once, read-only, from the old clan health file (``raid_member_activity``,
``player_directory`` and the clan and name of each ``player_snapshots`` row), then
written in the shared startup transaction after the profile copy. A raid line the
record already holds wins, and so does every line of a weekend still running at
freeze time: the old values are then only listed in the receipt. A weekend the
record never saw is written member-only and listed in ``old_raid_weekends``.
Only structural problems refuse the copy. Per account, consecutive
captures in one clan make one stay, first to last capture, and a stay ends at any
gap of more than 2 hours between two captures; a capture in two family clans at
once is left out, and the record's own membership takes over where it begins
(unless the account was away just before it). Names fill only accounts without one. Snapshots, reports, settings and wars
are copied elsewhere or stay with clan health.
"""

from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from itertools import groupby
from pathlib import Path
import sqlite3

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from . import now_us
from .api_fields import read_fields
from .legacy_links import ImportMismatch, digest
from .link_state import receipt
from .people import account_name
from .profile_import import row_digest
from .projections import canonical_json, nonnegative_int
from .raids import COLUMNS as LINE_COLUMNS
from .war_projection import api_time
from .wars import SECOND


TOPIC = "health_history"
COLLECTOR = "health_import"
ORIGIN = "legacy_health"
VERSIONS = {0, 1, 2}
# Old raid rows carry the weekend's end; Clash weekends run Friday to Monday, start to end.
WEEKEND = 3 * 24 * 3600 * SECOND
# Captures are hourly; further apart than this, the account was away in between.
MAX_GAP = 2 * 3600 * SECOND
RAID_VALUES = {"attacks": "attacks", "attack_limit": "attack_limit", "bonus_attack_limit": "bonus_attack_limit",
               "capital_loot": "loot"}
TABLES = {
    "raid_member_activity": (("weekend_id", "clan_code", "clan_tag", "end_ts", "player_tag", *RAID_VALUES.values()),
                             "clan_tag,end_ts,player_tag"),
    "player_directory": (("player_tag", "player_name"), "player_tag"),
    "player_snapshots": (("captured_ts", "clan_code", "player_tag", "player_name"), "player_tag,captured_ts,clan_code"),
}


@dataclass(frozen=True)
class HealthSnapshot:
    version: int
    digest: str
    frozen_at: int
    raids: dict
    directory: dict
    names: dict
    stays: dict
    counts: dict
    captures: dict
    cutoffs: dict


def record_memberships(connection):
    """Each account's first clan membership in the record; old stays end there."""
    return {row[0]: row[1] for row in connection.execute(
        "SELECT a.player_tag,MIN(m.from_at) FROM clan_membership m JOIN accounts a ON a.id=m.account_id GROUP BY a.id")}


def weekend_id(clan_tag, end):
    return f"RAID:{clan_tag}:{datetime.fromtimestamp(end, timezone.utc).strftime('%Y%m%dT%H%M%S.000Z')}"


def bad(table):
    raise ImportMismatch("bad_row:" + table)


def snapshot_health(health_path, clans, cutoffs, at=None):
    """Read the three old tables in one read transaction; the old file is never adopted or changed."""
    frozen_at = now_us() if at is None else at
    if not nonnegative_int(frozen_at):
        raise ValueError("invalid_freeze_time")
    tags = {code: normalize_player_tag(tag) for code, tag in clans.items()}
    hasher, counts = sha256(), Counter()
    raids, directory, names, stays = defaultdict(dict), {}, {}, {}
    captures = Counter()
    try:
        with closing(sqlite3.connect(Path(health_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
            source.row_factory = sqlite3.Row
            source.execute("PRAGMA query_only=ON")
            source.execute("PRAGMA temp_store=MEMORY")
            with sqlite_transaction(source):
                version = source.execute("PRAGMA user_version").fetchone()[0]
                if version not in VERSIONS:
                    raise ImportMismatch("unsupported_health_source_version")
                hasher.update(canonical_json(["health_history", version]).encode())
                for table, (columns, _) in TABLES.items():
                    if not set(columns) <= {row[1] for row in source.execute(f"PRAGMA table_info({table})")}:
                        raise ImportMismatch("legacy_schema_mismatch:" + table)

                def rows(table):
                    columns, order = TABLES[table]
                    for row in source.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}"):
                        counts[table] += 1
                        hasher.update((canonical_json([table, *row]) + "\n").encode())
                        yield row

                for row in rows("raid_member_activity"):
                    clan, tag, end = row["clan_tag"], row["player_tag"], row["end_ts"]
                    if (tags.get(row["clan_code"]) is None or tags[row["clan_code"]] != clan
                            or normalize_player_tag(tag) != tag or not nonnegative_int(end)
                            or row["weekend_id"] != weekend_id(clan, end)
                            or not all(nonnegative_int(row[name]) for name in RAID_VALUES.values())):
                        bad("raid_member_activity")
                    weekend = raids[clan, end * SECOND - WEEKEND, end * SECOND]
                    if tag in weekend:
                        bad("raid_member_activity")
                    weekend[tag] = {column: row[name] for column, name in RAID_VALUES.items()}
                for row in rows("player_directory"):
                    tag = row["player_tag"]
                    if normalize_player_tag(tag) != tag or not isinstance(row["player_name"], str) or not row["player_name"]:
                        bad("player_directory")
                    directory[tag] = row["player_name"]
                for tag, group in groupby(rows("player_snapshots"), key=lambda row: row["player_tag"]):
                    if normalize_player_tag(tag) != tag:
                        bad("player_snapshots")
                    stays[tag], names[tag] = account_stays(tag, group, tags, cutoffs.get(tag), captures)
    except sqlite3.Error as error:
        raise ImportMismatch("health_source_unreadable") from error
    for tag, name in directory.items():
        # The directory is the old file's own summary of its captures, so the two must agree.
        if tag not in names or names[tag][0] != name:
            raise ImportMismatch("directory_name_mismatch")
    return HealthSnapshot(1, hasher.hexdigest(), frozen_at, {key: dict(value) for key, value in raids.items()},
                          directory, names, stays, dict(counts), dict(captures), dict(cutoffs))


def account_stays(tag, group, tags, cutoff, captures):
    """One account's captures as stays, and its last name with the capture it was first seen at."""
    stays, current, after, name, previous = [], None, None, None, None
    for seconds, rows in groupby(group, key=lambda row: row["captured_ts"]):
        rows = list(rows)
        if (not nonnegative_int(seconds) or any(tags.get(row["clan_code"]) is None for row in rows)
                or any(not isinstance(row["player_name"], str) or not row["player_name"] for row in rows)):
            bad("player_snapshots")
        if len({row["player_name"] for row in rows}) > 1:
            raise ImportMismatch("conflicting_capture_names")
        stamp = seconds * SECOND
        captures["captures"] += 1
        if name is None or name[0] != rows[0]["player_name"]:
            name = (rows[0]["player_name"], stamp)
        # Missing captures are an absence, not another clan: a stay never runs across one.
        gap = previous is not None and stamp - previous > MAX_GAP
        previous = stamp
        if gap:
            current = None
        if len(rows) > 1:
            captures["two_clans"] += 1
            continue
        clan = tags[rows[0]["clan_code"]]
        if cutoff is not None and stamp >= cutoff:
            captures["after_record_membership"] += 1
            if after is None:
                after = clan if not gap else False
            continue
        captures["in_stays"] += 1
        if current is not None and current["clan_tag"] == clan:
            current["last_observed_at"] = current["to_at"] = stamp
            current["captures"] += 1
        else:
            current = {"clan_tag": clan, "from_at": stamp, "to_at": stamp, "last_observed_at": stamp, "captures": 1,
                       "end_basis": "last_capture"}
            stays.append(current)
    if current is not None and after == current["clan_tag"]:
        # Still in that clan when the record's own membership begins.
        current.update(to_at=cutoff, end_basis="record_membership")
    return stays, name


def record_weekends(connection):
    """(clan, start) -> end of every raid weekend the record holds."""
    return {(row[0], row[1]): api_time(read_fields(connection, "capital_raids", row[2]).get("/endTime"))
            for row in connection.execute("SELECT scope_id,source_at,record_scope FROM family_api_entities "
                                          "WHERE source_kind='capital_raids' AND source_at IS NOT NULL")}


def verify_health(connection, frozen, run, item, expected, checks):
    if checks["captures"]["captures"] != sum(checks["captures"][key] for key in (
            "in_stays", "two_clans", "after_record_membership")):
        raise ImportMismatch("source_rows_unaccounted:player_snapshots")
    if sum(checks["raid_lines"][key] for key in ("matched", "kept_record", "left_running", "imported")) != \
            frozen.counts.get("raid_member_activity", 0):
        raise ImportMismatch("source_rows_unaccounted:raid_member_activity")
    if sum(checks["names"].values()) != frozen.counts.get("player_directory", 0):
        raise ImportMismatch("source_rows_unaccounted:player_directory")
    for table, (count, fingerprint) in expected.items():
        rows = connection.execute(f"SELECT * FROM {table} WHERE source_run_id=? ORDER BY 1,2,3", (run,)).fetchall()
        if len(rows) != count or row_digest(rows) != fingerprint:
            raise ImportMismatch("health_readback_mismatch:" + table)
    names = connection.execute("SELECT COUNT(*) FROM account_aliases WHERE source_item_id=?", (item,)).fetchone()[0]
    if names != checks["names"]["added"]:
        raise ImportMismatch("health_readback_mismatch:account_aliases")
    if connection.execute("SELECT 1 FROM old_clan_stays s JOIN accounts a ON a.id=s.account_id "
                          "JOIN (SELECT account_id,MIN(from_at) AS first FROM clan_membership GROUP BY account_id) m "
                          "ON m.account_id=s.account_id WHERE s.source_run_id=? AND s.to_at>m.first", (run,)).fetchone():
        raise ImportMismatch("health_record_boundary")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise ImportMismatch("family_foreign_key_check")


def write_health(connection, frozen):
    """The health part of the startup copy, inside the caller's transaction; returns its result, marker written."""
    if record_memberships(connection) != frozen.cutoffs:
        raise ImportMismatch("record_membership_changed")
    item = receipt(connection, COLLECTOR, TOPIC, frozen.frozen_at, sum(frozen.counts.values()))
    run = connection.execute("SELECT run_id FROM collection_items WHERE id=?", (item,)).fetchone()[0]
    accounts = {row[0]: row[1] for row in connection.execute("SELECT player_tag,id FROM accounts")}
    if (frozen.stays.keys() | frozen.directory.keys()) - accounts.keys():
        raise ImportMismatch("snapshot_account_missing")
    created = []

    def raider(tag):
        # An account seen only in raid lines the copy writes; one in a line the record kept is not needed.
        if tag not in accounts:
            accounts[tag] = connection.execute(
                "INSERT INTO accounts(player_tag,first_observed_at,tracking_status) VALUES (?,NULL,'legacy_raid')",
                (tag,)).lastrowid
            created.append(tag)
        return accounts[tag]

    planned = {"raid_member_lines": [], "old_raid_weekends": [], "old_clan_stays": []}
    lines, weekends, differences = Counter(), Counter(), []
    known = record_weekends(connection)
    ends = {(clan, end): start for (clan, start), end in known.items()}
    for (clan, start, end), members in sorted(frozen.raids.items()):
        # The old loop keeps writing a running weekend; the live collector owns it.
        running = end > frozen.frozen_at
        if (clan, start) in known:
            if known[clan, start] != end:
                raise ImportMismatch("raid_weekend_end_mismatch")
            weekends["record"] += 1
            kind = "imported_record_weekends"
        elif (clan, end) in ends:
            raise ImportMismatch("raid_weekend_start_mismatch")
        else:
            weekends["running" if running else "old_only"] += 1
            kind = "imported_old_weekends"
            if not running:
                planned["old_raid_weekends"].append((clan, start, end, len(members), ORIGIN, run))
        for tag, values in sorted(members.items()):
            old = connection.execute(f"SELECT {','.join(LINE_COLUMNS)} FROM raid_member_lines "
                                     "WHERE clan_tag=? AND weekend_start=? AND player_tag=?", (clan, start, tag)).fetchone()
            stored = None if old is None else dict(zip(LINE_COLUMNS, old))
            if stored == values:
                lines["matched"] += 1
                continue
            if stored is not None or running:
                # The record collected its line at or after the weekend's end, or will: it wins.
                lines["kept_record" if stored is not None else "left_running"] += 1
                differences.append({"clan_tag": clan, "weekend_start": start, "player_tag": tag,
                                    "reason": "record_line" if stored is not None else "running_weekend",
                                    "record": stored, "old": values})
                continue
            # The old file kept no capture time; an ended weekend's values stand as of its end.
            planned["raid_member_lines"].append((clan, start, tag, raider(tag), *(values[c] for c in LINE_COLUMNS),
                                                 end, run))
            lines["imported"] += 1
            lines[kind] += 1
    for tag, stays in sorted(frozen.stays.items()):
        for stay in stays:
            planned["old_clan_stays"].append((accounts[tag], stay["clan_tag"], stay["from_at"], stay["to_at"],
                                              stay["last_observed_at"], stay["captures"], stay["end_basis"],
                                              "observed_hourly", ORIGIN, run))
    names = Counter()
    for tag, name in sorted(frozen.directory.items()):
        identifier = accounts[tag]
        alias = connection.execute("SELECT name FROM account_aliases WHERE account_id=? AND to_at IS NULL",
                                   (identifier,)).fetchone()
        current = alias[0] if alias else connection.execute(
            "SELECT current_name FROM accounts WHERE id=?", (identifier,)).fetchone()[0]
        if current is None:
            # From the capture that first showed this name, through the old file's last sight of it.
            account_name(connection, identifier, name, frozen.names[tag][1], item)
            names["added"] += 1
        else:
            names["equal" if current == name else "differing"] += 1
    expected = {}
    for table, rows in planned.items():
        rows.sort(key=lambda row: row[:3])
        if rows:
            connection.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in rows[0])})", rows)
        expected[table] = (len(rows), row_digest(rows))
    checks = {"source_rows": frozen.counts, "created_accounts": len(created),
              "raid_lines": {key: lines[key] for key in ("matched", "kept_record", "left_running", "imported",
                                                          "imported_record_weekends", "imported_old_weekends")},
              "raid_weekends": {key: weekends[key] for key in ("record", "old_only", "running")},
              # Old values the copy did not write, beside the record's, so each difference stays visible.
              "raid_differences": differences,
              "captures": {key: frozen.captures.get(key, 0) for key in (
                  "captures", "in_stays", "two_clans", "after_record_membership")},
              "stays": len(planned["old_clan_stays"]),
              "stays_ending_at_record": sum(row[6] == "record_membership" for row in planned["old_clan_stays"]),
              "names": {key: names[key] for key in ("equal", "differing", "added")},
              "inserted": {table: len(rows) for table, rows in planned.items()},
              "stay_basis": "consecutive_captures_one_clan; split_at_gaps_over_2_hours; two_clan_captures_left_out; ends_at_record_membership"}
    verify_health(connection, frozen, run, item, expected, checks)
    written = len(created) + names["added"] + sum(len(rows) for rows in planned.values())
    connection.execute("UPDATE collection_runs SET rows_written=? WHERE id=?", (written, run))
    connection.execute("UPDATE collection_items SET written_count=? WHERE id=?", (written, item))
    connection.execute("INSERT INTO imports VALUES (?,?,?,?,?,?,?,?)", (
        TOPIC, frozen.version, frozen.digest, frozen.frozen_at, digest(sorted(accounts.items())), frozen.frozen_at,
        canonical_json(checks), item))
    return {"imported": True, "checks": checks, "source_digest": frozen.digest, "frozen_at": frozen.frozen_at}


def snapshot_for_record(connection, health_path, clans):
    return snapshot_health(health_path, clans, record_memberships(connection))
