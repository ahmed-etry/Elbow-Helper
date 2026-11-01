"""Test-only rehearsal of the startup copy of the old wars, on copies of the old and family files.

Run from the repository root:
``python -m tests.data.war_import_rehearsal <data folder> <work folder> [report file]``.
The data folder holds ``clan_health/clan_health.db``, ``war/war_cache.json`` and
``family/family.sqlite3``. Everything is copied into the work folder first; the
originals are only read. It runs the copy the way the bot starts: a first start,
a second start on a fresh store (it must read nothing and add nothing), a first
start whose copy fails (nothing may be kept and startup goes on), and a first
start while the old bot writes (the copy must succeed with what it read).
"""

import asyncio
from collections import Counter
from contextlib import closing
from hashlib import sha256
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.reads.coverage import war_side_coverage
from elbow_helper.data.reads.wars import member_rows
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store import wars_import
from elbow_helper.data.store.legacy_links import ImportMismatch


def fingerprint(path):
    """Row counts and a content hash of every table, so 'adds nothing' is a comparison, not a claim."""
    with closing(sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)) as connection:
        result = {}
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            hasher, rows = sha256(), 0
            for row in connection.execute(f'SELECT * FROM "{table}"'):
                hasher.update(repr(row).encode("utf-8"))
                rows += 1
            result[table] = (rows, hasher.hexdigest())
        return result


def size(path):
    path = Path(path)
    return sum(candidate.stat().st_size for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
               if candidate.exists())


async def settle(path):
    store = FamilyStore(path)
    await store.initialize()
    await store.call(lambda connection: connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
    return store


def counts(path):
    with closing(sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)) as connection:
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
            "wars", "war_sides", "war_members", "war_attacks", "war_legacy_ids", "war_source_keys")}


def reason(connection, war, coverage, repaired):
    """Why the record's reads withhold a war's misses."""
    own = coverage["own"]
    unknown = connection.execute("SELECT COUNT(*) FROM war_members WHERE war_id=? AND side=(SELECT side FROM war_sides "
                                 "WHERE war_id=? AND clan_tag=?) AND (attacks_expected IS NULL OR attacks_used IS NULL)",
                                 (war["id"], war["id"], own["clan_tag"])).fetchone()[0]
    if war["state"] != "warEnded":
        return f"not ended ({war['state']})"
    if war["team_size"] is None:
        return f"lineup of {own['members']} is not a whole team"
    if own["members"] != war["team_size"]:
        return f"lineup has {own['members']} of {war['team_size']} members"
    if unknown:
        return f"{unknown} members without expected or used attacks"
    if coverage["unattributed_attacks"]:
        return "attacks without attacker or defender"
    if own["attacks_reported"] is None:
        return "attack total unknown"
    note = ", ended from the last check" if repaired else ""
    return f"{own['attacks_stored']} attacks saved of {own['attacks_reported']} reported{note}"


def misses(connection, repaired_ids):
    """Per clan, over the regular wars the copy touched: missed attacks the record's reads count, and why the rest
    withhold them."""
    result, withheld = {}, []
    for code, clan in CLANS.items():
        totals = Counter()
        for war in connection.execute(
                "SELECT DISTINCT w.* FROM wars w JOIN war_legacy_ids l ON l.war_id=w.id AND l.clan_code=? "
                "JOIN war_sides s ON s.war_id=w.id AND s.clan_tag=? WHERE w.type='REG' ORDER BY w.end_at",
                (code, clan.tag)).fetchall():
            coverage = war_side_coverage(connection, war["id"], clan.tag)
            rows = member_rows(connection, war, clan.tag, coverage)
            proven = bool(rows) and all(row["missed"] is not None for row in rows)
            totals["wars_with_misses_counted" if proven else "wars_without_proven_misses"] += 1
            totals["missed_attacks"] += sum(row["missed"] for row in rows if row["missed"] is not None)
            if not proven:
                withheld.append({"clan": code, "war_id": war["id"], "type_basis": war["type_basis"],
                                 "end": time.strftime("%Y-%m-%d", time.gmtime(war["end_at"] // 1_000_000)),
                                 "reason": reason(connection, war, coverage, war["id"] in repaired_ids)})
        result[code] = dict(totals)
    return result, withheld


def repaired(connection, listed):
    """How each war the copy ended from a last battle-day check reads in the record."""
    out = []
    for item in listed:
        war = connection.execute("SELECT * FROM wars WHERE id=?", (item["war_id"],)).fetchone()
        tags = [row[0] for row in connection.execute("SELECT clan_tag FROM war_sides WHERE war_id=?", (war["id"],))]
        clan = next(tag for tag in tags if tag in {c.tag for c in CLANS.values()})
        coverage = war_side_coverage(connection, war["id"], clan)
        rows = member_rows(connection, war, clan, coverage)
        out.append({"war_id": war["id"], "created": item["created"], "type_basis": war["type_basis"],
                    "state": war["state"], "lineup_complete": coverage["own"]["lineup_complete"],
                    "attacks_complete": coverage["own"]["attacks_complete"],
                    "missed": sum(row["missed"] or 0 for row in rows) if coverage["own"]["attacks_complete"] else None})
    return out


def collectors(store, health, cache):
    bot = SimpleNamespace(boot_complete=asyncio.Event(), guild_id=0, get_guild=lambda guild_id: None)
    return FamilyCollectors(bot, store, SimpleNamespace(get=AsyncMock()), CLANS.values(), old_wars=(health, cache))


class Lines(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.getMessage()}")


async def rehearse(data, work):
    data, work = Path(data), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    health, cache = work / "clan_health.db", work / "war_cache.json"
    if not health.exists():
        shutil.copyfile(data / "clan_health" / "clan_health.db", health)
    shutil.copyfile(data / "war" / "war_cache.json", cache)
    sources = (sha256(health.read_bytes()).hexdigest(), sha256(cache.read_bytes()).hexdigest())
    for name in ("run", "failure"):
        shutil.copyfile(data / "family" / "family.sqlite3", work / f"{name}.sqlite3")
    lines = Lines()
    logging.getLogger("elbow_helper.data.collectors.service").addHandler(lines)
    logging.getLogger("elbow_helper.data.collectors.service").setLevel(logging.INFO)
    report = {}
    path = work / "run.sqlite3"
    store = await settle(path)
    report["before"] = {"bytes": size(path), "rows": counts(path)}

    # First start.
    started = time.perf_counter()
    await collectors(store, health, cache)._copy_old_wars()
    report["first_start_seconds"] = round(time.perf_counter() - started, 2)
    report["first_start_log"] = lines.lines[:]
    await store.call(lambda connection: connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
    report["after"] = {"bytes": size(path), "rows": counts(path)}
    marker = await store.call(lambda c: c.execute("SELECT checks_json FROM imports WHERE topic=?",
                                                  (wars_import.TOPIC,)).fetchone())
    checks = json.loads(marker[0])
    report["checks"] = checks
    report["misses"], report["withheld"] = await store.call(
        misses, {war["war_id"] for war in checks["ended_after_last_check"]})
    report["ended_after_last_check"] = await store.call(repaired, checks["ended_after_last_check"])

    # Second start: a new process, so a new store; it must read nothing and add nothing.
    settled = fingerprint(path)
    lines.lines.clear()
    store = FamilyStore(path)
    started = time.perf_counter()
    with patch.object(wars_import, "snapshot_wars", side_effect=AssertionError("old files read")):
        await collectors(store, health, cache)._copy_old_wars()
    report["second_start_seconds"] = round(time.perf_counter() - started, 3)
    report["second_start_log"] = lines.lines[:]
    report["second_start_changed_nothing"] = fingerprint(path) == settled

    # A failing first start: a check fails after every war is written, so nothing may be kept.
    failure = work / "failure.sqlite3"
    store = await settle(failure)
    untouched = fingerprint(failure)
    lines.lines.clear()
    with patch.object(wars_import.Importer, "verify", side_effect=ImportMismatch("forced_check_failure")):
        await collectors(store, health, cache)._copy_old_wars()
    report["failure_log"] = lines.lines[:]
    report["failure_changed_nothing"] = fingerprint(failure) == untouched

    # The old bot writing while the copy runs: the copy keeps the snapshot it read and succeeds.
    writing = work / "writing.sqlite3"
    shutil.copyfile(data / "family" / "family.sqlite3", writing)
    shutil.copyfile(health, work / "writing_health.db")
    store = await settle(writing)
    real_write = wars_import.Importer.write

    def write_meanwhile(importer, events):
        with closing(sqlite3.connect(work / "writing_health.db", timeout=30)) as old, old:
            old.execute("UPDATE war_roster_members SET captured_ts=captured_ts+1")
            old.execute("UPDATE war_activity SET attacks_used=attacks_used")
        return real_write(importer, events)
    lines.lines.clear()
    with patch.object(wars_import.Importer, "write", write_meanwhile):
        await collectors(store, work / "writing_health.db", cache)._copy_old_wars()
    report["old_writer_log"] = lines.lines[:]
    report["old_writer_copy_matches_first_start"] = counts(writing) == report["after"]["rows"]
    report["old_files_unchanged"] = sources == (sha256(health.read_bytes()).hexdigest(),
                                                sha256(cache.read_bytes()).hexdigest())
    return report


if __name__ == "__main__":
    result = json.dumps(asyncio.run(rehearse(*sys.argv[1:3])), indent=1, sort_keys=True)
    if len(sys.argv) > 3:
        Path(sys.argv[3]).write_text(result, encoding="utf-8")
    else:
        print(result)
