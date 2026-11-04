"""Offline startup rehearsal and independent field-by-field reconciliation on supplied copies.

python -m tests.data.profile_import_rehearsal <data folder> <work folder>
"""

import asyncio
from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import closing
from hashlib import sha256
from itertools import groupby
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from unittest.mock import patch

from elbow_helper.data.store import FamilyStore, profile_import, ranked_import, wars_import
from elbow_helper.data.store.legacy_links import ImportMismatch
from elbow_helper.data.store.player_projection import COUNTERS, LEGEND_FAST, LEGEND_SLOW
from elbow_helper.data.store.profile_import_plan import HEALTH_FIELDS, TOTALS
from elbow_helper.data.store.projections import canonical_json
from elbow_helper.features.clan_health.api import _PET_NAME_KEYS, _unit_name_key
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .ranked_import_rehearsal import file_hash, startup
from .war_import_rehearsal import fingerprint, settle, size


def readonly(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA temp_store=MEMORY")
    return closing(c)


def independent_series(health, ranked, original, destination):
    """Compare every changed field/time/value, independent of importer plans and row packing."""
    with readonly(original) as record, readonly(health) as a, readonly(ranked) as b, readonly(destination) as copied:
        cuts = {}
        for table, column in (("player_counters", "sampled_at"), ("player_observations", "observed_at"), ("player_items", "observed_at")):
            for row in record.execute(f"SELECT a.player_tag,MIN(p.{column}) FROM {table} p JOIN accounts a ON a.id=p.account_id GROUP BY a.id"):
                cuts[row[0]] = min(cuts.get(row[0], row[1]), row[1])
        ranked_rows, spans = defaultdict(list), {}
        for row in b.execute("SELECT * FROM snapshots WHERE status='ok' ORDER BY player_tag,observed_at"):
            tag, stamp = row["player_tag"], ranked_import.timestamp(row["observed_at"])
            spans[tag] = (spans.get(tag, (stamp, stamp))[0], stamp)
            if stamp >= cuts.get(tag, 2**63 - 1):
                continue
            values = json.loads(row["fields_json"])
            legend = values.pop("legendStatistics", None)
            if legend is not None:
                values["legendStatistics"] = {k: v for k, v in legend.items() if k in LEGEND_FAST}
                values.update({"legendStatistics." + k: v for k, v in legend.items() if k in LEGEND_SLOW})
            ranked_rows[tag].append((stamp, values))
        expected, actual = {}, {}

        def trace(rows):
            last, hashes, counts = {}, {}, Counter()
            for stamp, values in sorted(rows, key=lambda row: row[0]):
                for key, value in sorted(values.items()):
                    if key not in last or last[key] != value:
                        hashes.setdefault(key, sha256()).update((canonical_json([stamp, value]) + "\n").encode())
                        counts[key] += 1
                        last[key] = value
            return {key: (counts[key], hasher.hexdigest()) for key, hasher in hashes.items()}

        tags_seen = set()
        for tag, group in groupby(a.execute("SELECT * FROM player_snapshots ORDER BY player_tag,captured_ts,clan_code"),
                                  key=lambda row: row["player_tag"]):
            rows, at_time = ranked_rows.get(tag, [])[:], defaultdict(lambda: defaultdict(set))
            for row in group:
                stamp = row["captured_ts"] * 1_000_000
                if stamp >= cuts.get(tag, 2**63 - 1):
                    continue
                values = {dest: row[source] for source, dest in HEALTH_FIELDS.items() if row[source] is not None}
                values.update({key: row[key] for key in TOTALS if row[key] is not None})
                if tag in spans and spans[tag][0] <= stamp <= spans[tag][1]:
                    values.pop("trophies", None)
                    values.pop("attackWins", None)
                for key, value in values.items():
                    at_time[stamp][key].add(value)
            for stamp, values in at_time.items():
                rows.append((stamp, {key: sorted(alternatives) if len(alternatives) > 1 else next(iter(alternatives))
                                     for key, alternatives in values.items()}))
            expected[tag] = trace(rows)
            tags_seen.add(tag)
        for tag in ranked_rows.keys() - tags_seen:
            expected[tag] = trace(ranked_rows[tag])
        imported = defaultdict(list)
        for row in copied.execute("SELECT p.*,a.player_tag FROM player_counters p JOIN accounts a ON a.id=p.account_id "
                                  "JOIN collection_runs r ON r.id=p.source_run_id WHERE r.collector='profile_import'"):
            presence = json.loads(row["field_presence_json"])
            values = {key: row[column] for i, (key, column) in enumerate(COUNTERS.items()) if presence["fields"] & (1 << i)}
            values.update(presence.get("conflicts", {}))
            if presence.get("legendStatistics"):
                values["legendStatistics"] = json.loads(row["legend_counters_json"])
            imported[row["player_tag"]].append((row["sampled_at"], values))
        for row in copied.execute("SELECT p.*,a.player_tag FROM player_observations p JOIN accounts a ON a.id=p.account_id "
                                  "JOIN collection_runs r ON r.id=p.source_run_id WHERE r.collector='profile_import'"):
            imported[row["player_tag"]].append((row["observed_at"], {row["field_key"]: json.loads(row["value_json"])}))
        for row in copied.execute("SELECT p.*,a.player_tag FROM old_profile_totals p JOIN accounts a ON a.id=p.account_id"):
            imported[row["player_tag"]].append((row["observed_at"], {key: row[key] for key in TOTALS if row[key] is not None}))
        actual = {tag: trace(rows) for tag, rows in imported.items()}
        expected = {tag: fields for tag, fields in expected.items() if fields}
        if expected != actual:
            differing = sum(expected.get(tag) != actual.get(tag) for tag in expected.keys() | actual.keys())
            raise AssertionError(f"independent_profile_series_differ:{differing}")
        return {"accounts": len(expected), "fields": sum(len(fields) for fields in expected.values()),
                "changes": sum(n for fields in expected.values() for n, _ in fields.values()), "equal": True}


def compare_item_totals(health, family):
    """Evaluate the record's last observed items at each overlapping A instant; report all differences."""
    with readonly(health) as old, readonly(family) as record:
        catalog = {r["id"]: dict(r) for r in record.execute("SELECT * FROM item_catalog")}
        items, counters, profiles = defaultdict(list), defaultdict(list), defaultdict(list)
        for row in record.execute("SELECT p.*,a.player_tag FROM player_items p JOIN accounts a ON a.id=p.account_id ORDER BY observed_at"):
            items[row["player_tag"]].append(dict(row))
        for row in record.execute("SELECT p.*,a.player_tag FROM player_counters p JOIN accounts a ON a.id=p.account_id ORDER BY sampled_at"):
            counters[row["player_tag"]].append(dict(row))
        for row in record.execute("SELECT scope_id,observed_at FROM api_records r JOIN api_field_changes f ON f.record_id=r.id "
                                  "WHERE r.source_kind='player_profile' GROUP BY scope_id,observed_at ORDER BY observed_at"):
            profiles[row[0]].append(row[1])
        stats = {key: Counter() for key in TOTALS}
        exact, eligible, unknown_units = 0, 0, set()
        for tag, group in groupby(old.execute("SELECT * FROM player_snapshots ORDER BY player_tag,captured_ts,clan_code"),
                                  key=lambda row: row["player_tag"]):
            if tag not in items:
                continue
            levels, values, item_index, counter_index, seen = {}, {}, 0, 0, set()
            for row in group:
                stamp = row["captured_ts"] * 1_000_000
                if stamp in seen or stamp < items[tag][0]["observed_at"]:
                    continue
                seen.add(stamp)
                poll_index = bisect_right(profiles[tag], stamp) - 1
                if poll_index < 0 or stamp - profiles[tag][poll_index] > 2 * 3600 * 1_000_000:
                    continue
                exact += profiles[tag][poll_index] == stamp
                eligible += 1
                while item_index < len(items[tag]) and items[tag][item_index]["observed_at"] <= stamp:
                    item = items[tag][item_index]
                    levels[item["item_id"]] = item["level"] if item["present"] else None
                    item_index += 1
                while counter_index < len(counters[tag]) and counters[tag][counter_index]["sampled_at"] <= stamp:
                    values.update(json.loads(counters[tag][counter_index]["achievement_values_json"]))
                    counter_index += 1
                sums = dict.fromkeys(TOTALS, 0)
                games = set()
                for identifier, level in levels.items():
                    item = catalog[identifier]
                    source, village = item["source_array"], item["village"]
                    if source == "achievements" and item["source_name"] == "Games Champion" and str(identifier) in values:
                        games.add(values[str(identifier)])
                    if level is None:
                        continue
                    if source == "heroes" and village in {"", "home"}:
                        sums["hero_sum"] += level
                    elif source == "heroEquipment":
                        sums["equipment_sum"] += level
                    elif source == "spells" and village in {"", "home"}:
                        sums["spell_sum"] += level
                    elif source == "troops" and village.lower() == "home":
                        # Use the old collector's exact game-unit classification for comparability.
                        pet = _unit_name_key(item["source_name"]) in _PET_NAME_KEYS
                        sums["pet_sum" if pet else "troop_sum"] += level
                        if item["classification"] is None:
                            unknown_units.add(identifier)
                sums["games_total"] = next(iter(games)) if len(games) == 1 else None
                for key, value in sums.items():
                    if value is None or row[key] is None:
                        stats[key]["not_comparable"] += 1
                    else:
                        delta = value - row[key]
                        stats[key]["equal" if delta == 0 else "different"] += 1
                        if delta:
                            stats[key]["record_lower" if delta < 0 else "record_higher"] += 1
                            stats[key]["max_absolute_difference"] = max(stats[key]["max_absolute_difference"], abs(delta))
        return {"overlap_samples": eligible, "exact_poll_times": exact, "unclassified_troop_catalog_items": len(unknown_units),
                "fields": stats, "basis": "record_item_state_as_of_old_capture; last_record_poll_within_two_hours",
                "pet_split": "old_collector_game_unit_classification; no catalog changes",
                "games": "comparable only when all observed Games Champion values agree"}


async def prior_copies(store, health, cache, ranked):
    await store.call(wars_import.copy_at_startup, health, cache)
    def write(c):
        frozen = ranked_import.snapshot_ranked(ranked)
        with sqlite_transaction(c, immediate=True):
            ranked_import.write_ranked(c, frozen, ranked_import.plan_ranked(c, frozen))
    await store.call(write)


async def rehearse(data, work):
    data, work = Path(data).resolve(), Path(work).resolve()
    if data == work or data in work.parents or not work.is_relative_to(Path.cwd()):
        raise ValueError("rehearsal_work_must_be_in_worktree_outside_inputs")
    work.mkdir(parents=True, exist_ok=True)
    health, ranked, family = [data / name for name in ("clan_health/clan_health.db", "ranked_stats/ranked.sqlite3", "family/family.sqlite3")]
    cache = data / "war/war_cache.json"
    if not cache.exists():
        cache = data.parent / "war/war_cache.json"
    inputs = (health, ranked, family, cache)
    hashes = await asyncio.to_thread(lambda: [file_hash(p) for p in inputs])
    report = {"data_date": "2026-10-31", "work": work.relative_to(Path.cwd()).as_posix()}
    report["totals_comparison"] = await asyncio.to_thread(compare_item_totals, health, family)
    for case in ("first", "live", "failure", "live_failure"):
        path = work / (case + ".sqlite3")
        if path.exists():
            raise FileExistsError(path)
        await asyncio.to_thread(shutil.copyfile, family, path)
        store = await settle(path)
        if case.startswith("live"):
            await prior_copies(store, health, cache, ranked)
            await store.call(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
        before = await asyncio.to_thread(fingerprint, path)
        before_size = await asyncio.to_thread(size, path)
        started = time.perf_counter()
        if "failure" in case:
            with patch.object(profile_import, "verify_profiles", side_effect=ImportMismatch("forced_after_profile_writes")):
                resumed = await startup(store, health, cache, ranked)
        else:
            resumed = await startup(store, health, cache, ranked)
        elapsed = round(time.perf_counter() - started, 3)
        await store.call(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
        after = await asyncio.to_thread(fingerprint, path)
        report[case] = {"seconds": elapsed, "before_bytes": before_size, "after_bytes": await asyncio.to_thread(size, path),
                        "bot_resumed": {"players", "wars", "battle_logs"} <= set(resumed), "unchanged": before == after}
        assert report[case]["bot_resumed"]
        if "failure" in case:
            assert before == after
        else:
            marker = await store.call(lambda c: c.execute("SELECT checks_json FROM imports WHERE topic=?", (profile_import.TOPIC,)).fetchone()[0])
            report[case]["checks"] = json.loads(marker)
            report[case]["independent"] = await asyncio.to_thread(independent_series, health, ranked, family, path)
            if case == "live":
                for table in before:
                    if table not in {"accounts", "player_counters", "player_observations", "old_profile_totals",
                                     "profile_history_coverage", "collection_runs", "collection_items", "imports"}:
                        assert before[table] == after[table], "profile copy changed " + table
                report[case]["other_facts_unchanged"] = True
            second_started = time.perf_counter()
            with patch.object(profile_import, "snapshot_for_record", side_effect=AssertionError("profile source read")), \
                    patch.object(ranked_import, "snapshot_ranked", side_effect=AssertionError("ranked source read")), \
                    patch.object(wars_import, "snapshot_wars", side_effect=AssertionError("war source read")):
                await startup(FamilyStore(path), health, cache, ranked)
            report[case]["second_seconds"] = round(time.perf_counter() - second_started, 3)
            assert after == await asyncio.to_thread(fingerprint, path)
            report[case]["second_unchanged"] = True
        (work / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(case, canonical_json({k: v for k, v in report[case].items() if k != "checks"}), flush=True)
    assert hashes == await asyncio.to_thread(lambda: [file_hash(p) for p in inputs])
    report["inputs_unchanged"] = True
    (work / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    asyncio.run(rehearse(sys.argv[1], sys.argv[2]))
