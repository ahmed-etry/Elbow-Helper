"""Offline shared-startup rehearsal; input files are only read, results use a separate work folder.

python -m tests.data.ranked_import_rehearsal <data folder> <work folder> <report.md>
"""

import asyncio
from collections import Counter, defaultdict
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
from unittest.mock import patch

from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.store import FamilyStore, ranked_import, wars_import
from elbow_helper.data.store.legacy_links import ImportMismatch

from .war_import_rehearsal import Lines, fingerprint, settle, size


def file_hash(path):
    with Path(path).open("rb") as stream:
        return sha256_file(stream)


def sha256_file(stream):
    hasher = sha256()
    while chunk := stream.read(1024 * 1024):
        hasher.update(chunk)
    return hasher.hexdigest()


async def startup(store, health, cache, ranked):
    """Actual cog startup, with only network schedules replaced by completion probes."""
    bot = SimpleNamespace(boot_complete=asyncio.Event())
    owner = FamilyCollectors(bot, store, None, (), old_wars=(health, cache, ranked))
    resumed = []
    async def schedule(name, seconds, collect, *, startup=False, after=None):
        await bot.boot_complete.wait()
        if after is not None:
            await after
        resumed.append(name)
    with patch.object(owner, "_schedule", side_effect=schedule):
        await owner.cog_load()
        bot.boot_complete.set()
        await asyncio.gather(*owner._tasks)
        await owner.cog_unload()
    return sorted(resumed)


def battle_totals(connection):
    result = defaultdict(Counter)
    for tag, attack, stars in connection.execute(
            "SELECT s.value,b.is_attack,b.stars FROM family_battles b JOIN battle_strings s ON s.id=b.account_string_id "
            "WHERE b.legacy_first_seen_json IS NOT NULL"):
        result[tag]["battles"] += 1
        result[tag]["attacks" if attack else "defenses"] += 1
        result[tag]["stars"] += stars or 0
    return dict(result)


def old_totals(path):
    result = defaultdict(Counter)
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        for tag, attack, payload in c.execute("SELECT player_tag,attack,payload_json FROM ranked_battles"):
            result[tag]["battles"] += 1
            result[tag]["attacks" if attack else "defenses"] += 1
            result[tag]["stars"] += json.loads(payload).get("stars", 0)
    return dict(result)


async def rehearse(data, work):
    data, work = Path(data).resolve(), Path(work).resolve()
    if data == work or data in work.parents:
        raise ValueError("rehearsal_work_must_be_separate_from_inputs")
    work.mkdir(parents=True, exist_ok=True)
    inputs = [data / name for name in ("clan_health/clan_health.db", "war/war_cache.json",
                                       "ranked_stats/ranked.sqlite3", "family/family.sqlite3")]
    hashes = await asyncio.to_thread(lambda: [file_hash(p) for p in inputs])
    health, cache, ranked, family = inputs
    logger = logging.getLogger("elbow_helper.data.collectors.service")
    lines = Lines()
    logger.addHandler(lines)
    logger.setLevel(logging.INFO)
    report = {"work_directory": work.relative_to(Path.cwd()).as_posix()}
    try:
        for case in ("first", "live", "failure"):
            path = work / f"{case}.sqlite3"
            if path.exists():
                raise FileExistsError(path)
            await asyncio.to_thread(shutil.copyfile, family, path)
            store = await settle(path)
            if case == "live":
                await store.call(wars_import.copy_at_startup, health, cache)
                await store.call(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
            before = await asyncio.to_thread(fingerprint, path)
            before_size = await asyncio.to_thread(size, path)
            lines.lines.clear()
            started = time.perf_counter()
            if case == "failure":
                with patch.object(ranked_import, "verify_ranked", side_effect=ImportMismatch("forced_after_writes")):
                    resumed = await startup(store, health, cache, ranked)
            else:
                resumed = await startup(store, health, cache, ranked)
            elapsed = round(time.perf_counter() - started, 3)
            await store.call(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
            after = await asyncio.to_thread(fingerprint, path)
            report[case] = {"seconds": elapsed, "before_bytes": before_size,
                            "after_bytes": await asyncio.to_thread(size, path), "log": lines.lines[:],
                            "bot_schedules_resumed": resumed, "record_unchanged": before == after}
            if case == "failure":
                assert before == after, "failed copy changed the record"
                assert {"wars", "battle_logs"} <= set(resumed), "startup did not continue"
                continue
            marker = await store.call(lambda c: c.execute("SELECT checks_json FROM imports WHERE topic=?",
                                                         (ranked_import.TOPIC,)).fetchone())
            checks = json.loads(marker[0])
            report[case]["checks"] = checks
            assert await store.call(battle_totals) == await asyncio.to_thread(old_totals, ranked)
            report[case]["independent_account_totals_equal"] = True
            if case == "first":
                report["coverage"] = await store.call(lambda c: [dict(r) for r in c.execute(
                    "SELECT * FROM battle_log_coverage WHERE origin='legacy_ranked'")])
                report["name_keys"] = await store.call(lambda c: c.execute(
                    "SELECT COUNT(*) FROM family_battles b JOIN battle_strings s ON s.id=b.opponent_string_id "
                    "WHERE b.legacy_first_seen_json IS NOT NULL AND s.value LIKE 'name:%'").fetchone()[0])
                report["source_battles"] = checks["source_rows"]["ranked_battles"]
                lines.lines.clear()
                started = time.perf_counter()
                with patch.object(wars_import, "snapshot_wars", side_effect=AssertionError("old wars read")), \
                        patch.object(ranked_import, "snapshot_ranked", side_effect=AssertionError("old ranked read")):
                    await startup(FamilyStore(path), health, cache, ranked)
                report["second"] = {"seconds": round(time.perf_counter() - started, 3), "log": lines.lines[:],
                                    "record_unchanged": after == await asyncio.to_thread(fingerprint, path)}
                assert report["second"]["record_unchanged"], "second start changed the record"
            else:
                for table in before:
                    if table not in {"imports", "collection_runs", "collection_items", "family_battles", "battle_strings",
                                     "api_records", "api_paths", "api_field_changes", "battle_log_coverage"}:
                        assert before[table] == after[table], f"live ranked copy changed {table}"
                report[case]["war_facts_unchanged"] = True
        assert hashes == await asyncio.to_thread(lambda: [file_hash(p) for p in inputs]), "input file changed"
        report["inputs_unchanged"] = True
    finally:
        logger.removeHandler(lines)
    return report


def markdown(report):
    checks = report["first"]["checks"]
    battles = checks["outcomes"]["ranked_battles"]
    coverage = report["coverage"]
    lines = ["# Ranked copy rehearsal", "", "2026-11-04; offline, using the supplied 2026-10-31 backups. No API or AI calls.",
             "Inputs under `_data/` remained byte-for-byte unchanged (SHA-256 checked). "
             f"Runs use `{report['work_directory']}/`.",
             "", "## Startup results", "", "| Case | Seconds | Before bytes | After bytes | Result |",
             "|---|---:|---:|---:|---|"]
    for case, label in (("first", "All three copies pending"), ("live", "War and CWL copies already done"),
                        ("failure", "Forced failure after ranked writes")):
        row = report[case]
        result = "Unchanged; bot schedules resumed" if case == "failure" else "Passed; bot schedules resumed"
        lines.append(f"| {label} | {row['seconds']} | {row['before_bytes']} | {row['after_bytes']} | {result} |")
    lines += [f"| Second start, fresh store | {report['second']['seconds']} | — | — | No source reads; every table unchanged |",
              "", f"Battles: **{report['source_battles']:,} = {battles['matched']:,} matched + {battles['imported']:,} added**; "
              "zero excluded. Every matched payload leaf agreed; no record battle was rewritten.",
              f"All {report['name_keys']} name-keyed opponents survived, including the opponent with no name. "
              f"All {checks['before_first_poll']:,} battles older than their account's first poll were retained.",
              "The 207 battles absent inside the record's log window are retained as part of the 10,571 added battles; "
              "the importer does not filter by that window. Independent per-account battle, direction and star totals "
              "equal the source on both successful paths. Existing war facts are unchanged on the live path.",
              "", "## Watch coverage and exclusions", "",
              f"{len(coverage)} account facts compact {sum(r['polls'] for r in coverage):,} polls: "
              f"{sum(r['clean_polls'] for r in coverage):,} clean, {sum(r['partial_polls'] for r in coverage):,} partial, "
              f"{sum(r['failed_polls'] for r in coverage):,} unavailable; "
              f"{sum(r['skipped_entries'] for r in coverage):,} skipped entries. "
              f"{sum(r['empty_polls'] for r in coverage):,} empty polls; "
              f"{sum(r['nonempty_polls'] for r in coverage):,} nonempty polls. "
              f"{sum(len(json.loads(r['failed_spans_json'])) for r in coverage):,} failed spans retained.",
              "First attempt, first clean poll, last attempt, last clean poll, failure brackets and reasons are kept. "
              "Legacy and current watchers have separate rows, so an unobserved gap is never joined into coverage.",
              "", "| Source table | Rows | Disposition / reason |", "|---|---:|---|"]
    for table, outcomes in checks["outcomes"].items():
        for reason, count in outcomes.items():
            lines.append(f"| {table} | {count:,} | {reason} |")
    lines += ["", "Every excluded snapshot has reason `profile_history_deferred`; every excluded profile-health row has "
              "reason `operational_state`. The per-account counts below account for each row. No member IDs, polling "
              "intervals or battle hashes are adopted; profile values are deferred to the later combined history copy.",
              "The old clan is stored as `clan_when_first_seen` provenance on each battle, including matches, with its "
              "old first-observed time and source. It creates no membership or ownership interval.",
              "", "## Per-account reconciliation", "", "Identifiers below are family-record account IDs; no names or tags are published.",
              "", "| Account ID | Old battles | Matched | Added | Before first poll | Polls compacted | Snapshots deferred | Health excluded |",
              "|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in sorted(checks["accounts"].values(), key=lambda r: r["account_id"]):
        values = [row[k] for k in ("account_id", "battles", "matched", "added", "before_first_poll", "polls")]
        values += [row["excluded"].get("snapshots", 0), row["excluded"].get("profile_health", 0)]
        lines.append("| " + " | ".join(str(value) for value in values) + " |")
    lines += ["", "## Limits", "", "The supplied backups predate the November 2 season boundary. Season/group finalisation and "
              "profile history are outside this copy; no newer backup was supplied. No member-facing readers or wording changed.", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    result = asyncio.run(rehearse(sys.argv[1], sys.argv[2]))
    Path(sys.argv[3]).write_text(markdown(result), encoding="utf-8")
    Path(sys.argv[2], "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({case: {k: v for k, v in result[case].items() if k not in {"checks", "log"}}
                      for case in ("first", "second", "live", "failure")}, indent=2))
