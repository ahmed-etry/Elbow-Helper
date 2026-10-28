"""Offline family-record estimate for 279 accounts and nine clans.

Compressed patches use captured synthetic shapes. Typed rows include SQLite
indexes and page slack. Donation counters use the player model's daytime and
nighttime activity rates, with all daily samples changing conservatively.
WAL and backup copies are separate working space.
"""

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from elbow_helper.data.store.api_fields import collect_fields
from elbow_helper.data.store.clans import _donations
from elbow_helper.data.store.projections import CLAN_COUNTER_FIELDS, canonical_json
from elbow_helper.data.store.schema import create_schema


BASE = 1_800_000_000_000_000
HOUR = 3_600_000_000
FIXTURES = Path(__file__).parents[1] / "fixtures" / "family_api"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["body"]


def _patches(kind, initial, changed):
    with closing(sqlite3.connect(":memory:")) as c:
        c.row_factory = sqlite3.Row
        create_schema(c)
        sizes = []
        for index, payload in enumerate((initial, changed), 1):
            at = BASE + index * 24 * HOUR
            c.execute("INSERT INTO collection_runs(id,collector,scope_kind,scheduled_at,started_at,status,expected) "
                      "VALUES (?,'size','synthetic',?,?,'ok',1)", (index, at, at))
            collect_fields(c, kind, "synthetic", payload, index, at)
            sizes.append(c.execute("SELECT LENGTH(patch_z) FROM api_field_changes ORDER BY source_run_id DESC LIMIT 1").fetchone()[0])
        checkpoint = c.execute("SELECT LENGTH(state_z) FROM api_records").fetchone()[0]
        # WITHOUT ROWID history has no second index. Include cell headers,
        # record/run IDs, timestamp, and 20% page slack in the estimate.
        return tuple(int((size + 40) * 1.2) for size in (*sizes, checkpoint))


def annual_estimate(source=None):
    player = _load("player_4.json")
    fast = deepcopy(player)
    for key in ("trophies", "donations", "donationsReceived", "attackWins", "warStars", "clanCapitalContributions"):
        fast[key] += 123
    initial_player, fast_patch, player_state = _patches("player_profile", player, fast)
    slow = deepcopy(fast)
    for row in slow["achievements"][:3]:
        row["value"] += 123
    for row in slow["troops"][:2]:
        row["level"] += 1
    _, daily_patch, _ = _patches("player_profile", fast, slow)
    players = 279 * (initial_player + player_state + 365 * daily_patch) + 674639 * fast_patch

    clan = _load("clan_1.json")
    changed_clan = deepcopy(clan)
    for row in changed_clan["memberList"][:max(1, len(clan["memberList"]) // 3)]:
        for key in ("donations", "donationsReceived", "trophies"):
            row[key] += 123
    initial_clan, clan_patch, clan_state = _patches("clan_profile", clan, changed_clan)
    clans = 9 * (initial_clan + clan_state + 8760 * clan_patch)

    war = _load("cwl_war_1.json")
    changed_war = deepcopy(war)
    member = next(row for row in changed_war["clan"]["members"] if row.get("attacks"))
    attack = deepcopy(member["attacks"][0])
    attack["order"] += 1000
    member["attacks"].append(attack)
    member["bestOpponentAttack"] = deepcopy(attack)
    changed_war["clan"]["attacks"] += 1
    changed_war["clan"]["stars"] += 3
    initial_war, attack_patch, war_state = _patches("cwl_war", war, changed_war)
    regular, cwl = 9 * 365 // 2, 9 * 12 * 7
    wars = (regular + cwl) * (initial_war + war_state) + (regular * 120 + cwl * 60) * attack_patch
    group = _load("cwl_group_1.json")
    changed_group = {**group, "state": "inWar"}
    initial_group, group_patch, group_state = _patches("cwl_group", group, changed_group)
    groups = 9 * 12 * (initial_group + group_state + 8 * group_patch)
    log = _load("war_log_1.json")
    initial_log, _, log_state = _patches("war_log", log, log)
    # Fifty entries per captured page; retain every new war-log entry once,
    # and reserve a matching checkpoint, including deleted-clan entries.
    logs = (regular + cwl) * (initial_log + log_state) // 50

    with closing(sqlite3.connect(":memory:")) as c:
        c.row_factory = sqlite3.Row
        create_schema(c)
        c.executemany("INSERT INTO accounts(id,player_tag,first_observed_at,tracking_status) VALUES (?,?,?,'observed')",
                      ((i, f"#SYNTHETIC{i}", BASE) for i in range(1, 280)))
        c.executemany("INSERT INTO clans(id,tag,name,is_family,wiring_revision) VALUES (?,?,?,1,1)",
                      ((i, f"#CLAN{i}", f"Synthetic clan {i}") for i in range(1, 10)))
        c.executemany("INSERT INTO collection_runs(id,collector,scope_kind,scheduled_at,started_at,status,expected) "
                      "VALUES (?,'clans','family_clans',?,?,'ok',18)",
                      ((i, BASE + i * HOUR // 4, BASE + i * HOUR // 4) for i in range(1, 673)))
        c.executemany("INSERT INTO collection_items(id,run_id,ordinal,source_kind,scope_id,status,started_at,fetched_at,committed_at,"
                      "returned_count,written_count,completeness_json) VALUES (?,?,?,?,?,'ok',?,?,?,31,31,?)",
                      ((i, (i - 1) // 18 + 1, (i - 1) % 18, "clan_profile" if i % 2 else "clan_roster", f"#CLAN{(i-1)%18//2+1}",
                        BASE + (i // 18) * HOUR // 4, BASE + (i // 18) * HOUR // 4, BASE + (i // 18) * HOUR // 4,
                        '{"complete":true}') for i in range(1, 672 * 18 + 1)))
        current = {account: {"donations": 100, "donationsReceived": 50, "trophies": 4000,
                             "builderBaseTrophies": 2000, "clanRank": (account - 1) % 31 + 1,
                             "previousClanRank": (account - 1) % 31 + 1} for account in range(1, 280)}
        for poll in range(672):
            hour = poll // 4
            for account, row in current.items():
                active = (hour % 24 == 0 or
                          6 <= hour % 24 <= 21 and (account + hour + hour // 24) % 3 == 0 or
                          not 6 <= hour % 24 <= 21 and (account + hour + hour // 24) % 24 == 0)
                if poll % 4 == 0 and active:
                    row["donations"] += 20
                    row["donationsReceived"] += 10
                    row["trophies"] += 5
                _donations(c, account, (account - 1) // 31 + 1, row,
                           BASE + poll * HOUR // 4, poll * 18 + ((account - 1) // 31) * 2 + 2)
        totals = canonical_json({key: clan.get(key) for key in CLAN_COUNTER_FIELDS})
        presence = canonical_json([key for key in CLAN_COUNTER_FIELDS if key in clan])
        c.executemany("INSERT INTO clan_counters VALUES (?,?,?,?,?)",
                      ((clan_id, BASE + hour * HOUR, totals, presence, hour * 4 * 18 + clan_id * 2 - 1)
                       for hour in range(168) for clan_id in range(1, 10)))
        c.commit()
        c.execute("VACUUM")
        def table_bytes(table):
            names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE tbl_name=?", (table,))]
            return c.execute(f"SELECT SUM(pgsize) FROM dbstat WHERE name IN ({','.join('?' for _ in names)})", names).fetchone()[0]
        donations = table_bytes("donation_snapshots") * 365 // 7
        receipts = (table_bytes("collection_items") + table_bytes("collection_runs")) * 365 // 7
        clan_counters = table_bytes("clan_counters") * 365 // 7
    estimate = {"generic_players": players, "generic_clans": clans, "generic_wars": wars,
                "generic_groups_and_logs": groups + logs, "donations": donations,
                "clan_receipts": receipts, "clan_counters": clan_counters,
                "other_facts_and_growth_reserve": 30_000_000}
    if source is not None:
        from .player_size import annual_size as player_size
        from .war_size import annual_size as war_size
        estimate["typed_players"] = player_size(source)[1]
        estimate["typed_wars"] = war_size(source)[1]
    return estimate
