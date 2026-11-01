"""Test-only rehearsal of the regular-war import on copies of the old and family files.

Run from the repository root:
``python -m tests.data.war_import_rehearsal <data folder> <work folder> [report file]``.
The data folder holds ``clan_health/clan_health.db``, ``war/war_cache.json`` and
``family/family.sqlite3``. Everything is copied into the work folder first; the
originals are only read. The import runs twice (the second run must add
nothing), then once more with a forced failure (nothing may be committed).
The startup never runs this.
"""

import asyncio
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from unittest.mock import patch

from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store import wars_import
from elbow_helper.data.store.legacy_links import ImportMismatch
from elbow_helper.data.store.wars_import import import_wars, snapshot_wars


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


async def rehearse(data, work):
    data, work = Path(data), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    health, cache = work / "clan_health.db", work / "war_cache.json"
    if not health.exists():
        shutil.copyfile(data / "clan_health" / "clan_health.db", health)
    shutil.copyfile(data / "war" / "war_cache.json", cache)
    report = {}
    for name in ("run", "failure"):
        shutil.copyfile(data / "family" / "family.sqlite3", work / f"{name}.sqlite3")
    path = work / "run.sqlite3"
    store = await settle(path)
    report["before"] = {"bytes": size(path), "rows": counts(path)}
    started = time.perf_counter()
    first = await store.call(import_wars, health, cache)
    report["first_seconds"] = round(time.perf_counter() - started, 2)
    await store.call(lambda connection: connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
    report["after"] = {"bytes": size(path), "rows": counts(path)}
    report["first"] = {key: first[key] for key in ("imported", "source_version", "source_digest")}
    report["checks"] = first["checks"]
    # A repeat adds nothing: unchanged sources are recognised, and a lost marker still adds nothing.
    settled = fingerprint(path)
    again = await store.call(import_wars, health, cache)
    report["second_imported"] = again["imported"]
    report["second_changed_nothing"] = fingerprint(path) == settled
    await store.call(lambda connection: (connection.execute("DELETE FROM imports WHERE topic=?", (wars_import.TOPIC,)),
                                         connection.commit()))
    markerless = fingerprint(path)
    unmarked = await store.call(import_wars, health, cache)
    bookkeeping = ("imports", "collection_runs", "collection_items")
    rest = fingerprint(path)
    report["markerless_repeat_imported"] = unmarked["imported"]
    report["markerless_repeat_changed"] = sorted(table for table in rest
                                                 if table not in bookkeeping and rest[table] != markerless[table])
    report["markerless_repeat_changed_nothing"] = not report["markerless_repeat_changed"]
    # A forced failure: the sources change while the import runs, so nothing may be kept.
    failure = work / "failure.sqlite3"
    store = await settle(failure)
    untouched = fingerprint(failure)
    real = snapshot_wars(health, cache)

    def changed(*args, **kwargs):
        return type(real)(real.version, "changed", real.frozen_at, real.rows, real.history)
    with patch.object(wars_import, "snapshot_wars", changed):
        try:
            await store.call(import_wars, health, cache, real)
            report["forced_failure"] = "not raised"
        except ImportMismatch as error:
            report["forced_failure"] = str(error)
    report["forced_failure_changed_nothing"] = fingerprint(failure) == untouched
    return report


if __name__ == "__main__":
    result = json.dumps(asyncio.run(rehearse(*sys.argv[1:3])), indent=1, sort_keys=True)
    if len(sys.argv) > 3:
        Path(sys.argv[3]).write_text(result, encoding="utf-8")
    else:
        print(result)
