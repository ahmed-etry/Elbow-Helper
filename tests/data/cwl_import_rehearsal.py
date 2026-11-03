"""Test-only rehearsal of the startup copy of the old CWL wars, on copies of the old and family files.

Run from the repository root:
``python -m tests.data.cwl_import_rehearsal <data folder> <work folder> [report file]``.
The data folder holds ``clan_health/clan_health.db``, ``war/war_cache.json`` and
``family/family.sqlite3``; everything is copied into the work folder first and
the originals are only read. The regular wars are copied first, as on the live
bot, so a start copies the league wars alone. Then: a second start on a new
store (it must read nothing and add nothing) and a first start, both parts at
once, whose league copy fails (nothing may be kept and startup goes on). Per
league week it sets the old store's numbers beside what the record now holds.
"""

import asyncio
from collections import Counter, defaultdict
from contextlib import closing
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from unittest.mock import patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.data.reads.coverage import war_side_coverage
from elbow_helper.data.reads.wars import member_rows
from elbow_helper.data.store import FamilyStore, wars_import, wars_import_cwl
from elbow_helper.data.store.legacy_links import ImportMismatch

from .war_import_rehearsal import Lines, collectors, fingerprint, settle, size


def counts(path):
    with closing(sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)) as connection:
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
            "wars", "war_sides", "war_members", "war_attacks", "war_legacy_ids", "war_source_keys", "cwl_seasons",
            "cwl_clans", "cwl_players", "cwl_rounds", "cwl_season_sources", "war_collection_state")}


def old_numbers(health):
    """Per (clan code, season key): what the old store's readers count."""
    result = defaultdict(Counter)
    with closing(sqlite3.connect(f"file:{Path(health).as_posix()}?mode=ro", uri=True)) as old:
        weeks = {(code, war_id): season for war_id, code, season in old.execute(
            "SELECT war_id,clan_code,cwl_season FROM wars WHERE war_type='CWL'")}
        # Health reports read activity rows; attendance and the bonus read the final lineup and its attacks.
        for war_id, code, expected, used in old.execute(
                "SELECT war_id,clan_code,attacks_expected,attacks_used FROM war_activity WHERE war_type='CWL'"):
            numbers = result[code, weeks.get((code, war_id))]
            numbers["health_joined"] += 1
            numbers["health_missed"] += max(0, expected - used)
        for war_id, code, expected, used in old.execute(
                "SELECT war_id,clan_code,attacks_expected,attacks_used FROM war_roster_members WHERE war_id LIKE 'CWL:%'"):
            numbers = result[code, weeks.get((code, war_id))]
            numbers["attendance_rows"] += 1
            numbers["attendance_missed"] += max(0, expected - used)
        for war_id, code, stars, townhall in old.execute(
                "SELECT war_id,clan_code,stars,defender_townhall FROM war_attacks WHERE war_type='CWL'"):
            numbers = result[code, weeks.get((code, war_id))]
            numbers["bonus_attacks"] += 1
            numbers["bonus_stars"] += stars
            numbers["bonus_triples"] += stars == 3
            numbers["bonus_defender_townhalls"] += townhall
    return result


def record_numbers(connection):
    """The same numbers from the record, through its own reads, per (clan code, old season key)."""
    result = defaultdict(Counter)
    for code, clan in CLANS.items():
        for war in connection.execute(
                "SELECT w.*,json_extract(s.group_metadata_json,'$.legacy.season_key') AS season FROM wars w "
                "JOIN cwl_rounds r ON r.war_id=w.id JOIN cwl_seasons s ON s.id=r.season_id "
                "JOIN cwl_season_sources src ON src.season_id=s.id AND src.clan_tag=? ORDER BY w.end_at",
                (clan.tag,)).fetchall():
            numbers = result[code, war["season"]]
            coverage = war_side_coverage(connection, war["id"], clan.tag)
            rows = member_rows(connection, war, clan.tag, coverage)
            numbers["attendance_rows"] += len(rows)
            numbers["attendance_missed"] += sum(row["missed"] or 0 for row in rows)
            numbers["missed_unknown"] += sum(row["missed"] is None for row in rows)
            numbers["opponent_misses_readable"] += sum(
                row["missed"] is not None for row in member_rows(
                    connection, war, coverage["opponent"]["clan_tag"],
                    war_side_coverage(connection, war["id"], coverage["opponent"]["clan_tag"])))
            for stars, townhall in connection.execute(
                    "SELECT a.stars,d.townhall FROM cwl_attacks a JOIN war_members d ON d.id=a.defender_member_id "
                    "WHERE a.war_id=?", (war["id"],)):
                numbers["bonus_attacks"] += 1
                numbers["bonus_stars"] += stars
                numbers["bonus_triples"] += stars == 3
                numbers["bonus_defender_townhalls"] += townhall or 0
    return result


async def rehearse(data, work):
    data, work = Path(data), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    health, cache = work / "clan_health.db", work / "war_cache.json"
    if not health.exists():
        shutil.copyfile(data / "clan_health" / "clan_health.db", health)
    shutil.copyfile(data / "war" / "war_cache.json", cache)
    for name in ("run", "failure"):
        shutil.copyfile(data / "family" / "family.sqlite3", work / f"{name}.sqlite3")
    lines = Lines()
    logger = logging.getLogger("elbow_helper.data.collectors.service")
    logger.addHandler(lines)
    logger.setLevel(logging.INFO)
    report = {}

    # The live bot already copied its regular wars.
    path = work / "run.sqlite3"
    store = await settle(path)
    started = time.perf_counter()
    await store.call(wars_import.import_wars, health, cache)
    report["regular_copy_seconds"] = round(time.perf_counter() - started, 2)
    await store.call(lambda connection: connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
    report["before"] = {"bytes": size(path), "rows": counts(path)}

    # First start: the league wars.
    started = time.perf_counter()
    await collectors(store, health, cache)._copy_old_wars()
    report["first_start_seconds"] = round(time.perf_counter() - started, 2)
    report["first_start_log"] = lines.lines[:]
    await store.call(lambda connection: connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
    report["after"] = {"bytes": size(path), "rows": counts(path)}
    marker = await store.call(lambda c: c.execute("SELECT checks_json FROM imports WHERE topic=?",
                                                  (wars_import_cwl.TOPIC,)).fetchone())
    report["checks"] = json.loads(marker[0]) if marker else None
    old, new = old_numbers(health), await store.call(record_numbers)
    report["numbers"] = [{"clan": code, "season_key": season, "old": dict(old[code, season]),
                          "record": dict(new.get((code, season), {}))} for code, season in sorted(
                              old, key=lambda key: (str(key[1]), key[0]))]
    report["collector_jobs_after"] = await store.call(lambda c: [tuple(r) for r in c.execute(
        "SELECT source_kind,enabled,COUNT(*) FROM war_collection_state GROUP BY 1,2 ORDER BY 1,2")])

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

    # The same checked copy again without its marker: every week is already there, so it adds nothing.
    await store.call(lambda c: (c.execute("DELETE FROM imports WHERE topic=?", (wars_import_cwl.TOPIC,)), c.commit()))
    rerun = await store.call(wars_import_cwl.import_cwl_wars, health)
    after = fingerprint(path)
    report["rerun_without_marker"] = {
        "inserted": rerun["checks"]["inserted"], "outcomes": rerun["checks"]["outcomes"],
        "facts_unchanged": all(after[table] == settled[table] for table in settled
                               if table not in ("imports", "collection_runs", "collection_items"))}

    # A failing first start with both parts pending: the league check fails after everything is written.
    failure = work / "failure.sqlite3"
    store = await settle(failure)
    untouched = fingerprint(failure)
    lines.lines.clear()
    with patch.object(wars_import_cwl.CwlImporter, "verify", side_effect=ImportMismatch("forced_check_failure")):
        await collectors(store, health, cache)._copy_old_wars()
    report["failure_log"] = lines.lines[:]
    report["failure_changed_nothing"] = fingerprint(failure) == untouched
    return report


if __name__ == "__main__":
    result = json.dumps(asyncio.run(rehearse(*sys.argv[1:3])), indent=1, sort_keys=True, default=str)
    if len(sys.argv) > 3:
        Path(sys.argv[3]).write_text(result, encoding="utf-8")
    else:
        print(result)
