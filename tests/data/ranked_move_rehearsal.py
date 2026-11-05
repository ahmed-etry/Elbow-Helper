"""Offline rehearsal of the ranked move: old-store counts against the record's ranked read.

python -m tests.data.ranked_move_rehearsal <data folder> <work folder> <report.md>

The data folder holds the backups (clan_health/, war/, ranked_stats/, family/); they are only
read. The family record is copied into the work folder, the startup copies run there, and the
same ranked questions (members, accounts, clans; 7 and 14 days) are answered both ways. The old
answer is what the old store held for the selection, the part of the old reader that did not
ask Clash. No API or AI calls.
"""

import asyncio
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import sys

from elbow_helper.configuration.clans import CLANS
from elbow_helper.data.reads import RankedReads

from .ranked_import_rehearsal import file_hash, startup
from .war_import_rehearsal import settle

SECOND = 1_000_000
WINDOWS = (7, 14)


def micros(value):
    return round(value.timestamp() * SECOND)


def old_store(path):
    return closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True))


def old_selection(path):
    with old_store(path) as c:
        members = sorted({row[0] for row in c.execute("SELECT DISTINCT member_id FROM snapshots")})
        accounts = sorted({row[0] for row in c.execute("SELECT DISTINCT player_tag FROM snapshots")})
        end = max(c.execute("SELECT MAX(observed_at) FROM battle_log_polls").fetchone()[0],
                  c.execute("SELECT MAX(observed_at) FROM snapshots").fetchone()[0])
    return members, accounts, datetime.fromisoformat(end)


def old_counts(path, selector, values, after, before):
    """Per account (attacks, defenses) the old store held for the selection, as its window read them."""
    column = {"member_ids": "member_id", "account_tags": "player_tag", "clan_tags": "clan_tag"}[selector]
    counts = Counter()
    with old_store(path) as c:
        for tag, attack, value in c.execute(
                f"SELECT player_tag,attack,{column} FROM ranked_battles WHERE battle_at>=? AND battle_at<?",
                (after.isoformat(timespec="microseconds"), before.isoformat(timespec="microseconds"))):
            if value in values:
                counts[tag, bool(attack)] += 1
    return counts


async def new_counts(reads, selector, values, after, before):
    counts, rows = Counter(), {}
    result = await reads.window(selector, values, after=micros(after), before=micros(before),
                                limit=10_000, include_battles=True)
    for row in result["accounts"]:
        rows[row["player_tag"]] = row
        for battle in row["battles"]:
            counts[row["player_tag"], battle["payload"]["attack"]] += 1
    return counts, rows


def cause(tag, old, new, rows, selector):
    """Why one account's counts differ, from facts the two sides keep."""
    row = rows.get(tag)
    if row is None:
        return "not selected by the record (owner now, or clan membership at battle time)"
    record_only = sum(not battle["old_store"] for battle in row["battles"])
    if record_only and new - old == record_only:
        return "battles only the record holds (read by the new collector)"
    if selector == "clan_tags":
        return "clan at battle time: record membership where the roster was watched, not the first-seen clan"
    return "old-store battles the record lacks, or record battles the old store lacked"


async def rehearse(data, work):
    data, work = Path(data).resolve(), Path(work).resolve()
    if data == work or data in work.parents:
        raise ValueError("rehearsal_work_must_be_separate_from_inputs")
    work.mkdir(parents=True, exist_ok=True)
    inputs = [data / name for name in ("clan_health/clan_health.db", "war/war_cache.json",
                                       "ranked_stats/ranked.sqlite3", "family/family.sqlite3")]
    hashes = await asyncio.to_thread(lambda: [file_hash(p) for p in inputs])
    health, cache, ranked, family = inputs
    path = work / "moved.sqlite3"
    if path.exists():
        raise FileExistsError(path)
    await asyncio.to_thread(shutil.copyfile, family, path)
    store = await settle(path)
    await startup(store, health, cache, ranked)
    reads = RankedReads(store)
    members, accounts, end = await asyncio.to_thread(old_selection, ranked)
    selections = {"member_ids": members, "account_tags": accounts,
                  "clan_tags": [clan.tag for clan in CLANS.values()]}
    report = {"end": end.isoformat(), "cases": []}
    for days in WINDOWS:
        after, before = end - timedelta(days=days), end
        for selector in ("account_tags", "member_ids", "clan_tags"):
            values = selections[selector]
            old = await asyncio.to_thread(old_counts, ranked, selector, set(values), after, before)
            new, rows = await new_counts(reads, selector, values, after, before)
            tags = sorted({tag for tag, _ in old} | {tag for tag, _ in new})
            differences = Counter()
            for tag in tags:
                pair = ((old[tag, True], old[tag, False]), (new[tag, True], new[tag, False]))
                if pair[0] != pair[1]:
                    differences[cause(tag, sum(pair[0]), sum(pair[1]), rows, selector)] += 1
            report["cases"].append({
                "days": days, "selector": selector, "selected": len(values),
                "old": {"attacks": sum(v for (t, a), v in old.items() if a), "defenses": sum(v for (t, a), v in old.items() if not a)},
                "new": {"attacks": sum(v for (t, a), v in new.items() if a), "defenses": sum(v for (t, a), v in new.items() if not a)},
                "accounts_compared": len(tags), "accounts_differing": sum(differences.values()),
                "causes": dict(differences.most_common())})
    assert hashes == await asyncio.to_thread(lambda: [file_hash(p) for p in inputs]), "input file changed"
    report["inputs_unchanged"] = True
    return report


def markdown(report):
    lines = ["# Ranked move rehearsal", "", f"Windows end at the old store's last read, {report['end']}. Offline; no API calls.",
             "", "| Days | Selection | Selected | Old attacks/defenses | New attacks/defenses | Accounts differing | Causes |",
             "|---:|---|---:|---|---|---:|---|"]
    for case in report["cases"]:
        causes = "; ".join(f"{count}: {text}" for text, count in case["causes"].items()) or "none"
        lines.append(f"| {case['days']} | {case['selector']} | {case['selected']} | "
                     f"{case['old']['attacks']}/{case['old']['defenses']} | {case['new']['attacks']}/{case['new']['defenses']} | "
                     f"{case['accounts_differing']} | {causes} |")
    return "\n".join(lines + [""])


if __name__ == "__main__":
    result = asyncio.run(rehearse(sys.argv[1], sys.argv[2]))
    Path(sys.argv[3]).write_text(markdown(result), encoding="utf-8")
    Path(sys.argv[2], "move.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(markdown(result))
