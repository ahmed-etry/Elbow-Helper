"""Materialize one synthetic year to measure SQLite pages, including indexes."""

from contextlib import closing
import json
import sqlite3

from elbow_helper.data.store.schema import create_schema


BASE = 1_800_000_000_000_000
HOUR = 3_600_000_000
DAY = 24 * HOUR


def annual_size(source):
    # The annual model uses real projected row shapes from the collector test.
    # One third change each of 16 daytime hours; 1/24 change overnight. Daily
    # progress changes three achievement keys. Items change twice/account/day.
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        create_schema(connection)
        connection.executemany(
            "INSERT INTO accounts(id,player_tag,current_name,first_observed_at,tracking_status) VALUES (?,?,?,?,'observed')",
            [(index, f"#synthetic-{index}", f"Synthetic account {index}", BASE) for index in range(1, 280)])
        connection.executemany(
            "INSERT INTO collection_runs(id,collector,scope_kind,scheduled_at,started_at,finished_at,status,expected,attempted,succeeded) "
            "VALUES (?,'players','family_and_linked_accounts',?,?,?,'ok',279,279,279)",
            [(index + 1, BASE + index * HOUR, BASE + index * HOUR, BASE + index * HOUR + 1) for index in range(8760)])
        catalog = source.execute("SELECT * FROM item_catalog").fetchall()
        connection.executemany("INSERT INTO item_catalog VALUES (?,?,?,?,?,?,?,?)", [tuple(row) for row in catalog])
        items = source.execute("SELECT * FROM player_items WHERE observed_at=(SELECT MIN(observed_at) FROM player_items)").fetchall()
        connection.executemany("INSERT INTO player_items VALUES (?,?,?,?,?,?,?,?,?)",
                               [(account, row["item_id"], BASE, row["level"], row["max_level"], row["static_fields_json"], 1, None, 1)
                                for account in range(1, 280) for row in items])
        connection.execute("CREATE TEMP TABLE templates AS SELECT * FROM player_items WHERE account_id=1")
        connection.execute(f"""WITH RECURSIVE seq(n) AS (
            VALUES(0) UNION ALL SELECT n+1 FROM seq WHERE n<203669)
            INSERT INTO player_items SELECT n%279+1,t.item_id,
                {BASE}+(n/279/2)*{DAY}+{HOUR},10+(n/279/208),20,t.static_fields_json,1,
                {BASE}+(n/279/2)*{DAY},MIN((n/279/2+1)*24+1,8760)
            FROM seq JOIN templates t ON t.item_id=(n/279)%208+1""")
        sample = source.execute("SELECT * FROM player_counters ORDER BY sampled_at LIMIT 1").fetchone()
        achievement_keys = list(json.loads(sample["achievement_values_json"]))[:3]
        # The full achievement baseline appears once; later patches contain
        # three numeric catalogue keys, never the complete achievement map.
        connection.execute(f"""WITH RECURSIVE seq(n) AS (
            VALUES(0) UNION ALL SELECT n+1 FROM seq WHERE n<{279 * 24 * 365 - 1})
            INSERT INTO player_counters SELECT n%279+1,{BASE}+(n/279)*{HOUR},
                5000+(n/279)%700,5700,4000,4200,1500+(n/279)/24,
                100+(n/279)/24,10,123456+(n/279)*10,(n/279)%20000,(n/279)%10000,
                ?,CASE WHEN n/279=0 THEN ? WHEN (n/279)%24=0
                    THEN json_object(?,10000+n/279/24,?,20000+n/279/24,?,30000+n/279/24) ELSE '{{}}' END,
                ?,?,'{{}}',n/279+1 FROM seq WHERE (n/279)%24=0 OR
                ((n/279)%24 BETWEEN 6 AND 21 AND ((n%279+1)+(n/279)+(n/279/24))%3=0) OR
                ((n/279)%24 NOT BETWEEN 6 AND 21 AND ((n%279+1)+(n/279)+(n/279/24))%24=0)""",
                           (sample["legend_counters_json"], sample["achievement_values_json"], *achievement_keys,
                            sample["transient_item_fields_json"], sample["field_presence_json"]))
        fields = source.execute("SELECT * FROM player_observations WHERE observed_at=(SELECT MIN(observed_at) FROM player_observations)").fetchall()
        connection.executemany("INSERT INTO player_observations VALUES (?,?,?,?,?,?)",
                               [(account, row["field_key"], BASE, row["value_json"], None, 1)
                                for account in range(1, 280) for row in fields])
        connection.execute(f"""WITH RECURSIVE seq(n) AS (
            VALUES(0) UNION ALL SELECT n+1 FROM seq WHERE n<20366)
            INSERT INTO player_observations SELECT n%279+1,'expLevel',{BASE}+(n/279+1)*5*{DAY}-{HOUR},
                CAST(251+n/279 AS TEXT),{BASE}+(n/279)*5*{DAY},MIN((n/279+1)*120+1,8760) FROM seq""")
        # One alias receipt and one merged coverage span/account. Failures are
        # additional; a healthy year's quiet successes have no item receipts.
        completeness = source.execute("SELECT completeness_json FROM player_coverage LIMIT 1").fetchone()[0]
        state = source.execute("SELECT * FROM player_collection_state LIMIT 1").fetchone()
        connection.executemany("INSERT INTO player_coverage VALUES (?,?,?,?,?,?)",
                               [(account, BASE, BASE + 8759 * HOUR, completeness, 1, 8760) for account in range(1, 280)])
        connection.executemany(
            "INSERT INTO player_collection_state(account_id,next_due_at,last_good_json,slow_sampled_at,progress_sampled_at,achievement_values_json) "
            "VALUES (?,?,?,?,?,?)", [(account, BASE + 8760 * HOUR, state["last_good_json"], BASE + 8759 * HOUR,
                                     BASE + 8736 * HOUR, state["achievement_values_json"]) for account in range(1, 280)])
        connection.executemany(
            "INSERT INTO collection_items(id,run_id,ordinal,source_kind,account_id,status,started_at) VALUES (?,1,?,'player_profile',?,'ok',?)",
            [(account, account - 1, account, BASE) for account in range(1, 280)])
        connection.executemany("INSERT INTO account_aliases(account_id,name,search_key,from_at,source_item_id) VALUES (?,?,?,?,?)",
                               [(account, f"Synthetic account {account}", f"synthetic account {account}", BASE, account) for account in range(1, 280)])
        connection.commit()
        connection.execute("DROP TABLE templates")
        connection.execute("VACUUM")
        tables = ("player_items", "player_counters", "player_observations", "collection_runs", "collection_items", "player_coverage")
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        counts["daily_progress"] = connection.execute("SELECT COUNT(*) FROM player_counters WHERE achievement_values_json!='{}'").fetchone()[0]
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        return counts, pages * page_size
