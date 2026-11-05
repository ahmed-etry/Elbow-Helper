"""Offline startup rehearsal of the clan health history copy on supplied copies, checked independently.

python -m tests.data.health_copy_rehearsal <data folder> <work folder>

The data folder holds copies in the bot's layout (clan_health/clan_health.db, family/family.sqlite3,
war/war_cache.json, ranked_stats/ranked.sqlite3); they are only read. Each case starts from its own copy
of the family file in the work folder: every copy pending, earlier copies already done, a forced failure,
and a second start after each successful one.
"""

import asyncio
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from unittest.mock import patch

from elbow_helper.data.store import FamilyStore, health_import, profile_import, ranked_import, wars_import
from elbow_helper.data.store.legacy_links import ImportMismatch
from elbow_helper.data.store.projections import canonical_json
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .ranked_import_rehearsal import file_hash, startup
from .war_import_rehearsal import fingerprint, settle

# The map's counts for the 31 October copy; stays are split at gaps of more than 2 hours.
EXPECTED = {"raid_lines": {"matched": 45, "imported": 4088}, "names": {"equal": 449, "differing": 4, "added": 103},
            "two_clans": 28, "stays": 11492, "stays_ending_at_record": 278, "created_accounts": 3, "raid_weekends": {"record": 119, "old_only": 71, "running": 0},
            "raid_kept": {"kept_record": 0, "left_running": 0}}
WEEKEND = 3 * 24 * 3600
MAX_GAP = 2 * 3600


def independent(health, path):
    """Old rows against what the record now holds, by plain SQL over both files."""
    with closing(sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)) as c:
        c.execute("ATTACH DATABASE ? AS old", (f"file:{Path(health).as_posix()}?mode=ro",))
        raids = c.execute(
            "SELECT COUNT(*),SUM(l.player_tag IS NOT NULL AND l.attacks=o.attacks AND l.attack_limit=o.attack_limit "
            "AND l.bonus_attack_limit=o.bonus_attack_limit AND l.capital_loot=o.loot) FROM old.raid_member_activity o "
            "LEFT JOIN raid_member_lines l ON l.clan_tag=o.clan_tag AND l.player_tag=o.player_tag "
            "AND l.weekend_start=(o.end_ts-?)*1000000", (WEEKEND,)).fetchone()
        captures = c.execute("SELECT COUNT(*) FROM (SELECT DISTINCT player_tag,captured_ts FROM old.player_snapshots)").fetchone()[0]
        two = c.execute("SELECT COUNT(*) FROM (SELECT 1 FROM old.player_snapshots GROUP BY player_tag,captured_ts "
                        "HAVING COUNT(*)>1)").fetchone()[0]
        after = c.execute(
            "SELECT COUNT(*) FROM (SELECT p.player_tag,p.captured_ts FROM old.player_snapshots p JOIN accounts a "
            "ON a.player_tag=p.player_tag JOIN (SELECT account_id,MIN(from_at) AS first FROM clan_membership "
            "GROUP BY account_id) m ON m.account_id=a.id WHERE p.captured_ts*1000000>=m.first "
            "GROUP BY p.player_tag,p.captured_ts HAVING COUNT(*)=1)").fetchone()[0]
        in_stays = c.execute("SELECT SUM(captures) FROM old_clan_stays").fetchone()[0]
        overlaps = c.execute("SELECT COUNT(*) FROM old_clan_stays a JOIN old_clan_stays b ON a.account_id=b.account_id "
                             "AND a.from_at<b.from_at AND b.from_at<=a.to_at").fetchone()[0]
        # Two captures of one account more than 2 hours apart: no stay may cover both.
        across_gaps = c.execute(
            "WITH c AS (SELECT DISTINCT player_tag,captured_ts AS t FROM old.player_snapshots), "
            "g AS (SELECT player_tag,t,LAG(t) OVER (PARTITION BY player_tag ORDER BY t) AS p FROM c) "
            "SELECT COUNT(*) FROM g JOIN accounts a ON a.player_tag=g.player_tag JOIN old_clan_stays s "
            "ON s.account_id=a.id AND s.from_at<=g.p*1000000 AND s.to_at>=g.t*1000000 WHERE g.t-g.p>?",
            (MAX_GAP,)).fetchone()[0]
        unnamed = c.execute("SELECT COUNT(*) FROM old.player_directory d JOIN accounts a ON a.player_tag=d.player_tag "
                            "WHERE a.current_name IS NULL").fetchone()[0]
        renamed = c.execute("SELECT COUNT(*) FROM old.player_directory d JOIN accounts a ON a.player_tag=d.player_tag "
                            "WHERE a.current_name IS NOT d.player_name").fetchone()[0]
        missing = c.execute("SELECT COUNT(*) FROM (SELECT DISTINCT player_tag FROM old.raid_member_activity UNION "
                            "SELECT player_tag FROM old.player_directory) o LEFT JOIN accounts a "
                            "ON a.player_tag=o.player_tag WHERE a.id IS NULL").fetchone()[0]
    result = {"old_raid_rows": raids[0], "raid_rows_equal_in_record": raids[1], "captures": captures,
              "two_clan_captures": two, "captures_after_record_membership": after, "captures_in_stays": in_stays,
              "overlapping_stays": overlaps, "stays_across_gaps": across_gaps, "directory_accounts_without_name": unnamed,
              "directory_names_differing": renamed, "old_accounts_missing": missing}
    assert raids[0] == raids[1], result
    assert captures == in_stays + two + after, result
    assert overlaps == 0 and across_gaps == 0 and unnamed == 0 and missing == 0, result
    return result


def compare(checks):
    found = {"raid_lines": {key: checks["raid_lines"][key] for key in ("matched", "imported")},
             "names": checks["names"], "two_clans": checks["captures"]["two_clans"],
             "stays": checks["stays"], "stays_ending_at_record": checks["stays_ending_at_record"],
             "created_accounts": checks["created_accounts"], "raid_weekends": checks["raid_weekends"],
             "raid_kept": {key: checks["raid_lines"][key] for key in ("kept_record", "left_running")}}
    assert len(checks["raid_differences"]) == sum(found["raid_kept"].values()), found
    assert found == EXPECTED, found
    return True


async def earlier_copies(store, health, cache, ranked):
    await store.call(wars_import.copy_at_startup, health, cache)

    def write(c):
        frozen = ranked_import.snapshot_ranked(ranked)
        profiles = profile_import.snapshot_for_record(c, health, ranked)
        with sqlite_transaction(c, immediate=True):
            ranked_import.write_ranked(c, frozen, ranked_import.plan_ranked(c, frozen))
            profile_import.write_profiles(c, profiles)
    await store.call(write)


async def rehearse(data, work):
    data, work = Path(data).resolve(), Path(work).resolve()
    if data == work or data in work.parents:
        raise ValueError("rehearsal_work_must_be_outside_inputs")
    work.mkdir(parents=True, exist_ok=True)
    health, family, cache, ranked = (data / name for name in (
        "clan_health/clan_health.db", "family/family.sqlite3", "war/war_cache.json", "ranked_stats/ranked.sqlite3"))
    inputs = (health, family, cache, ranked)
    hashes = await asyncio.to_thread(lambda: [file_hash(p) for p in inputs])
    report = {}
    for case in ("first", "later", "failure"):
        path = work / (case + ".sqlite3")
        if path.exists():
            raise FileExistsError(path)
        await asyncio.to_thread(shutil.copyfile, family, path)
        store = await settle(path)
        if case == "later":
            await earlier_copies(store, health, cache, ranked)
            await store.call(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
        before = await asyncio.to_thread(fingerprint, path)
        started = time.perf_counter()
        if case == "failure":
            with patch.object(health_import, "verify_health", side_effect=ImportMismatch("forced_after_health_writes")):
                resumed = await startup(store, health, cache, ranked)
        else:
            resumed = await startup(store, health, cache, ranked)
        await store.call(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
        after = await asyncio.to_thread(fingerprint, path)
        result = report[case] = {"seconds": round(time.perf_counter() - started, 1),
                                 "bot_resumed": {"players", "wars", "battle_logs"} <= set(resumed)}
        assert result["bot_resumed"]
        if case == "failure":
            result["unchanged"] = before == after
            assert result["unchanged"]
        else:
            markers = await store.call(lambda c: {row[0]: json.loads(row[1]) for row in c.execute(
                "SELECT topic,checks_json FROM imports")})
            checks = markers[health_import.TOPIC]
            result.update(checks=checks, topics=sorted(markers), matches_map=compare(checks),
                          independent=await asyncio.to_thread(independent, health, path))
            changed = sorted(table for table in after if before.get(table) != after[table])
            result["tables_changed"] = changed
            if case == "later":
                assert set(changed) <= {"accounts", "account_aliases", "raid_member_lines", "old_raid_weekends",
                                        "old_clan_stays", "collection_runs", "collection_items", "imports"}, changed
            second = time.perf_counter()
            with patch.object(health_import, "snapshot_for_record", side_effect=AssertionError("health source read")), \
                    patch.object(profile_import, "snapshot_for_record", side_effect=AssertionError("profile source read")), \
                    patch.object(ranked_import, "snapshot_ranked", side_effect=AssertionError("ranked source read")), \
                    patch.object(wars_import, "snapshot_wars", side_effect=AssertionError("war source read")):
                await startup(FamilyStore(path), health, cache, ranked)
            result["second_seconds"] = round(time.perf_counter() - second, 1)
            result["second_unchanged"] = after == await asyncio.to_thread(fingerprint, path)
            assert result["second_unchanged"]
        (work / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(case, canonical_json({k: v for k, v in result.items() if k != "checks"}), flush=True)
    report["inputs_unchanged"] = hashes == await asyncio.to_thread(lambda: [file_hash(p) for p in inputs])
    assert report["inputs_unchanged"]
    (work / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    asyncio.run(rehearse(sys.argv[1], sys.argv[2]))
