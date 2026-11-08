"""Materialize one synthetic year of war storage to measure SQLite pages, indexes included.

Nine clans with thirty-player sides: a regular war every two days per clan and
seven league rounds a month (regular wars normally pause for league season and
family mirrors share one war, so both make this conservative). Every war keeps
its final roster, both sides and every attack. League groups list fifty players
for each of eight clans. Run headers: one per five-minute pass all year, one per
minute on eleven league days a month, and eight exact end checks per regular
war. One logical item in a hundred fails and keeps its receipt.
"""

from contextlib import closing
import sqlite3

from elbow_helper.data.store.schema import create_schema


BASE = 1_800_000_000_000_000
MINUTE = 60_000_000
DAY = 1440 * MINUTE
CLANS, SIZE, SEASONS, LEAGUE_DAYS = 9, 30, 12, 11
REGULAR = CLANS * 365 // 2
LEAGUE = CLANS * SEASONS * 7
RUNS = 288 * 365 + (1440 - 288) * LEAGUE_DAYS * SEASONS + REGULAR * 8
ITEMS = (CLANS * 289) * 365 + (CLANS * 288 + 1440 * CLANS) * LEAGUE_DAYS * SEASONS
FAILURES = ITEMS // 100


def _insert(connection, table, template, rows):
    columns = list(template)
    connection.executemany(f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                           ([{**template, **row}[column] for column in columns] for row in rows))


def _template(source, table):
    return dict(source.execute(f"SELECT * FROM {table} LIMIT 1").fetchone())


def annual_size(source):
    """Copy real projected row shapes from `source`; return counts and bytes."""
    shapes = {table: _template(source, table) for table in (
        "wars", "war_sides", "war_members", "war_attacks", "cwl_seasons", "cwl_clans", "cwl_players",
        "collection_items", "war_collection_state")}
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        create_schema(connection)
        connection.executemany(
            "INSERT INTO accounts(id,player_tag,current_name,first_observed_at,tracking_status) VALUES (?,?,?,?,'observed')",
            [(index, f"#ACCOUNT{index:03d}", f"Synthetic account {index}", BASE) for index in range(1, 280)])
        connection.executemany(
            "INSERT INTO collection_runs(id,collector,scope_kind,scheduled_at,started_at,finished_at,status,"
            "expected,attempted,succeeded,rows_written) VALUES (?,'wars','family_wars_and_leagues',?,?,?,'ok',9,9,9,0)",
            ((run, BASE + run * MINUTE, BASE + run * MINUTE, BASE + run * MINUTE + 2_000_000) for run in range(1, RUNS + 1)))
        _insert(connection, "collection_items", {**shapes["collection_items"], "id": None}, (
            {"run_id": index * RUNS // FAILURES + 1, "ordinal": 0, "scope_id": "#FAMILY0%d" % (index % CLANS),
             "started_at": BASE + index * MINUTE} for index in range(FAILURES)))
        wars = REGULAR + LEAGUE
        _insert(connection, "wars", shapes["wars"], (
            {"id": war + 1, "type": "CWL" if war >= REGULAR else "REG",
             "war_tag": f"#W{war:08d}" if war >= REGULAR else None,
             "preparation_at": BASE + war * DAY // 5, "start_at": BASE + war * DAY // 5 + DAY,
             "end_at": BASE + war * DAY // 5 + 2 * DAY, "source_run_id": war * RUNS // wars + 1}
            for war in range(wars)))
        connection.executemany("INSERT INTO war_source_keys VALUES (?,?,?)", (
            (kind, f"#FAMILY0{war % CLANS}:#OPP{war:05d}:{BASE + war * DAY // 5 + index}", war + 1)
            for war in range(wars) for index, kind in enumerate(("end", "preparation", "cwl" if war >= REGULAR else "war_log"))))
        _insert(connection, "war_sides", shapes["war_sides"], (
            {"war_id": war + 1, "side": side, "clan_tag": f"#FAMILY0{war % CLANS}" if side == 0 else f"#OPP{war:05d}",
             "source_run_id": war * RUNS // wars + 1} for war in range(wars) for side in (0, 1)))
        _insert(connection, "war_members", shapes["war_members"], (
            {"id": war * 2 * SIZE + index + 1, "war_id": war + 1, "side": index // SIZE,
             "player_tag": f"#{war:05d}{index:03d}P", "account_id": (war * SIZE + index) % 279 + 1 if index < SIZE else None,
             "source_map_position": index % SIZE + 1, "source_run_id": war * RUNS // wars + 1}
            for war in range(wars) for index in range(2 * SIZE)))
        _insert(connection, "war_attacks", shapes["war_attacks"], (
            {"war_id": war + 1, "attack_order": order + 1,
             "attacker_member_id": war * 2 * SIZE + order % (2 * SIZE) + 1,
             "defender_member_id": war * 2 * SIZE + (order + SIZE) % (2 * SIZE) + 1,
             "source_run_id": war * RUNS // wars + 1}
            for war in range(wars) for order in range(2 * SIZE * (1 if war >= REGULAR else 2))))
        groups = CLANS * SEASONS
        _insert(connection, "cwl_seasons", shapes["cwl_seasons"], (
            {"id": group + 1, "season_key": f"2027-{group // CLANS + 1:02d}", "group_key": f"group-{group}",
             "source_run_id": group * RUNS // groups + 1} for group in range(groups)))
        connection.executemany("INSERT INTO cwl_season_sources VALUES (?,?)",
                               ((group + 1, f"#FAMILY0{group % CLANS}") for group in range(groups)))
        _insert(connection, "cwl_clans", shapes["cwl_clans"], (
            {"season_id": group + 1, "clan_tag": f"#LEAGUE{clan}{group:03d}", "source_run_id": group * RUNS // groups + 1}
            for group in range(groups) for clan in range(8)))
        _insert(connection, "cwl_players", shapes["cwl_players"], (
            {"season_id": group + 1, "clan_tag": f"#LEAGUE{clan}{group:03d}", "player_tag": f"#{group:03d}{clan}{index:02d}L",
             "account_id": (group * 50 + index) % 279 + 1 if clan == 0 else None, "source_run_id": group * RUNS // groups + 1}
            for group in range(groups) for clan in range(8) for index in range(50)))
        connection.executemany("INSERT INTO cwl_rounds VALUES (?,?,?,?,?)", (
            (group + 1, index // 4 + 1, f"#R{group:03d}{index:02d}X",
             REGULAR + group * 7 + index // 4 + 1 if index % 4 == 0 else None, group * RUNS // groups + 1)
            for group in range(groups) for index in range(28)))
        _insert(connection, "war_collection_state", shapes["war_collection_state"], (
            {"source_kind": kind, "scope_id": scope, "war_id": None}
            for kind, scope in [(kind, f"#FAMILY0{clan}") for clan in range(CLANS)
                                for kind in ("current_war", "war_log", "cwl_group")]
            + [("cwl_war", f"#R{group:03d}{index:02d}X") for group in range(groups) for index in range(28)]))
        connection.commit()
        connection.execute("VACUUM")
        tables = ("collection_runs", "collection_items", "wars", "war_sides", "war_members", "war_attacks",
                  "war_source_keys", "cwl_players", "cwl_rounds", "war_collection_state")
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        return counts, pages * connection.execute("PRAGMA page_size").fetchone()[0]
