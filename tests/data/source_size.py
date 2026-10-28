"""Reproducible first-year estimates, including lossless leaves and indexes.

Rates and compact battle table sizes are anonymized measurements from the
31 captured battle logs, 278 profiles, ranked groups and 108 completed raid
weekends. Four ranked reads/day conservatively change every standings row;
personal log growth rounds the measured battle rate up per read. Reference
growth reserves daily changes because only one catalogue capture is available.
No raw member data is required to reproduce the estimate.
"""

from contextlib import closing
from copy import deepcopy
import json
import math
from pathlib import Path
import sqlite3

from elbow_helper.data.store.api_fields import collect_fields
from elbow_helper.data.store.schema import create_schema


FIXTURES = Path(__file__).parents[1] / "fixtures" / "family_sources"
RATES = json.loads((FIXTURES.parent / "family_source_rates.json").read_text(encoding="utf-8"))


def _sample(kind):
    for path in sorted(FIXTURES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["kind"] == kind:
            return data["body"]
    raise ValueError(kind)


def _sizes(kind, raw, changed):
    with closing(sqlite3.connect(":memory:")) as c:
        c.row_factory = sqlite3.Row
        create_schema(c)
        for run, payload in enumerate((raw, changed), 1):
            c.execute("INSERT INTO collection_runs(id,collector,scope_kind,scheduled_at,started_at,status,expected) "
                      "VALUES (?,'size','synthetic',?,?,'ok',1)", (run, run, run))
            collect_fields(c, kind, "synthetic", payload, run, run)
        state = c.execute("SELECT LENGTH(state_z) FROM api_records").fetchone()[0]
        patches = [r[0] for r in c.execute("SELECT LENGTH(patch_z) FROM api_field_changes ORDER BY source_run_id")]
        initial, patch = patches[0], patches[-1] if len(patches) > 1 else 0
        # Allow for the record's scope and unique index, entity identity and
        # primary key, leaf history IDs/timestamps, plus 25% page slack.
        return int((state + initial + 700) * 1.25), int((patch + 48) * 1.25) if patch else 0


def annual_estimate(*, busiest=False):
    battle_rate = RATES["battle_rate_maximum" if busiest else "battle_rate_median"]
    history = _sample("league_history")["items"]
    per_season = sum(_sizes("league_history", row, row)[0] for row in history) // len(history)

    ranked = _sample("ranked_group")
    changed = deepcopy(ranked)
    for row in changed["members"]:
        row["leagueTrophies"] += 17
        row["attackWinCount"] += math.ceil(battle_rate / 4)
    if changed["attackLogs"]:
        for index in range(math.ceil(battle_rate / 4)):
            changed["attackLogs"].append({**changed["attackLogs"][0], "creationTime": f"2026-10-28T01:02:{index:02d}Z"})
    group_initial, group_patch = _sizes("ranked_group", ranked, changed)

    raid = _sample("capital_raids")["items"][0]
    changed = deepcopy(raid)
    changed["totalAttacks"] += 1
    changed["capitalTotalLoot"] += 1234
    changed["members"][0]["attacks"] += 1
    changed["members"][0]["capitalResourcesLooted"] += 1234
    district = changed["attackLog"][0]["districts"][0]
    district["attacks"].append(deepcopy(district["attacks"][0]))
    district["attackCount"] += 1
    raid_initial, raid_attack = _sizes("capital_raids", raid, changed)

    references = 0
    for path in FIXTURES.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["kind"] not in {"catalogue", "goldpass", "legend_seasons"}:
            continue
        rows = data["body"].get("items", [data["body"]])
        for row in rows:
            changed = {**row, "future": 1}
            initial, patch = _sizes(data["kind"], row, changed)
            references += initial + 365 * patch
    # Five collector run rows, checks and failure headroom. Successful items
    # have bounded checkpoints rather than one receipt per account per poll.
    # Reserve two runs per scheduled slot for accounts becoming due on either
    # side of a 30-second scheduler tick, plus final ranked-season checks.
    receipts = int(((24 + 4 + 24 + 1 + 1) * 365 * 2 + 52) * 180 * 1.25)
    return {"battle_logs": int(279 * battle_rate * 365 * RATES["compact_battle_bytes_with_indexes"] * 1.25),
            "league_history": 279 * 52 * per_season,
            "ranked_groups": RATES["current_ranked_accounts"] * (52 * group_initial + (4 * 365 + 52) * group_patch),
            "capital_raids": int(9 * 52 * (raid_initial + RATES["raid_attacks_maximum" if busiest else "raid_attacks_median"] * raid_attack)),
            "reference_data": references,
            "source_runs_and_failure_reserve": receipts}
