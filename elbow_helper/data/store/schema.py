"""Versioned, additive family-record schema. Times are UTC microseconds."""

import sqlite3

from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration


# Later subjects add migrations; they must not rewrite this baseline.
SCHEMA_VERSION = 3
STATEMENTS = (
    """CREATE TABLE collection_runs (
        id INTEGER PRIMARY KEY, collector TEXT NOT NULL, scope_kind TEXT NOT NULL,
        scheduled_at INTEGER NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER,
        status TEXT NOT NULL, expected INTEGER NOT NULL, attempted INTEGER NOT NULL DEFAULT 0,
        succeeded INTEGER NOT NULL DEFAULT 0, empty INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
        rows_written INTEGER NOT NULL DEFAULT 0, origin_attempts INTEGER, cache_hits INTEGER)""",
    "CREATE INDEX runs_collector ON collection_runs(collector,started_at)",
    "CREATE INDEX runs_status ON collection_runs(status,started_at)",
    """CREATE TABLE members (
        id INTEGER PRIMARY KEY, discord_id INTEGER NOT NULL UNIQUE,
        current_username TEXT, display_name TEXT, nick TEXT,
        first_observed_at INTEGER NOT NULL, last_observed_at INTEGER NOT NULL,
        bot INTEGER, presence_status TEXT NOT NULL)""",
    """CREATE TABLE accounts (
        id INTEGER PRIMARY KEY, player_tag TEXT NOT NULL UNIQUE, current_name TEXT,
        first_observed_at INTEGER NOT NULL, last_profile_at INTEGER,
        tracking_status TEXT NOT NULL)""",
    """CREATE TABLE collection_items (
        id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        ordinal INTEGER NOT NULL, source_kind TEXT NOT NULL, scope_id TEXT,
        account_id INTEGER REFERENCES accounts(id), status TEXT NOT NULL,
        http_status INTEGER, error_code TEXT, started_at INTEGER NOT NULL,
        fetched_at INTEGER, committed_at INTEGER, oldest_source_at INTEGER,
        newest_source_at INTEGER, returned_count INTEGER,
        written_count INTEGER NOT NULL DEFAULT 0, completeness_json TEXT NOT NULL DEFAULT '{}',
        attempts INTEGER, transport_observation_id TEXT, UNIQUE(run_id,ordinal))""",
    "CREATE INDEX items_account ON collection_items(account_id,source_kind,fetched_at)",
    "CREATE INDEX items_scope ON collection_items(scope_id,source_kind,fetched_at)",
    """CREATE TABLE member_aliases (
        member_id INTEGER NOT NULL REFERENCES members(id), kind TEXT NOT NULL,
        name TEXT NOT NULL, search_key TEXT NOT NULL, from_at INTEGER NOT NULL, to_at INTEGER,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        end_source_item_id INTEGER REFERENCES collection_items(id),
        CHECK(to_at IS NULL OR to_at >= from_at), PRIMARY KEY(member_id,kind,from_at))""",
    "CREATE INDEX member_alias_search ON member_aliases(search_key,member_id)",
    "CREATE UNIQUE INDEX member_alias_open ON member_aliases(member_id,kind) WHERE to_at IS NULL",
    """CREATE TABLE guild_membership (
        id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id),
        from_at INTEGER NOT NULL, to_at INTEGER, discord_joined_at INTEGER,
        previous_good_at INTEGER, end_previous_good_at INTEGER, last_observed_at INTEGER NOT NULL,
        start_basis TEXT NOT NULL, end_basis TEXT,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        end_source_item_id INTEGER REFERENCES collection_items(id),
        CHECK(to_at IS NULL OR to_at >= from_at), UNIQUE(member_id,from_at))""",
    "CREATE UNIQUE INDEX guild_open ON guild_membership(member_id) WHERE to_at IS NULL",
    "CREATE INDEX guild_member ON guild_membership(member_id,to_at)",
    """CREATE TABLE member_roles (
        id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id),
        role_id INTEGER NOT NULL, from_at INTEGER NOT NULL, to_at INTEGER,
        previous_good_at INTEGER, end_previous_good_at INTEGER, last_observed_at INTEGER NOT NULL,
        observation_basis TEXT NOT NULL,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        end_source_item_id INTEGER REFERENCES collection_items(id),
        CHECK(to_at IS NULL OR to_at >= from_at))""",
    "CREATE UNIQUE INDEX role_open ON member_roles(member_id,role_id) WHERE to_at IS NULL",
    "CREATE INDEX roles_member ON member_roles(member_id,to_at)",
    "CREATE INDEX roles_history ON member_roles(role_id,from_at,to_at)",
    """CREATE TABLE account_aliases (
        account_id INTEGER NOT NULL REFERENCES accounts(id), name TEXT NOT NULL,
        search_key TEXT NOT NULL, from_at INTEGER NOT NULL, to_at INTEGER,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        end_source_item_id INTEGER REFERENCES collection_items(id),
        CHECK(to_at IS NULL OR to_at >= from_at), PRIMARY KEY(account_id,from_at))""",
    "CREATE INDEX account_alias_search ON account_aliases(search_key,account_id)",
    "CREATE UNIQUE INDEX account_alias_open ON account_aliases(account_id) WHERE to_at IS NULL",
    """CREATE TABLE account_links (
        id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
        member_id INTEGER NOT NULL REFERENCES members(id), from_at INTEGER NOT NULL, to_at INTEGER,
        previous_good_at INTEGER, end_previous_good_at INTEGER, last_observed_at INTEGER NOT NULL,
        basis TEXT NOT NULL, operation_id TEXT,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        end_source_item_id INTEGER REFERENCES collection_items(id),
        CHECK(to_at IS NULL OR to_at >= from_at), UNIQUE(account_id,from_at))""",
    "CREATE UNIQUE INDEX link_open ON account_links(account_id) WHERE to_at IS NULL",
    "CREATE INDEX link_owner ON account_links(member_id,from_at,to_at)",
    """CREATE TABLE account_link_metadata (
        account_id INTEGER PRIMARY KEY REFERENCES accounts(id), player_name_last_seen TEXT,
        last_seen_clan_tag TEXT, last_seen_clan_code TEXT, last_seen_role TEXT,
        observed_at INTEGER, basis TEXT NOT NULL,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id))""",
    """CREATE TABLE clans (
        id INTEGER PRIMARY KEY, tag TEXT NOT NULL UNIQUE, family_code TEXT UNIQUE,
        name TEXT NOT NULL, is_family INTEGER NOT NULL, wiring_revision INTEGER NOT NULL)""",
    """CREATE TABLE clan_observations (
        clan_id INTEGER NOT NULL REFERENCES clans(id), observed_at INTEGER NOT NULL,
        metadata_json TEXT NOT NULL, previous_good_at INTEGER,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        PRIMARY KEY(clan_id,observed_at))""",
    "CREATE INDEX observations_time ON clan_observations(observed_at)",
    """CREATE TABLE clan_counters (
        clan_id INTEGER NOT NULL REFERENCES clans(id), sampled_at INTEGER NOT NULL,
        totals_json TEXT NOT NULL, field_presence_json TEXT NOT NULL,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        PRIMARY KEY(clan_id,sampled_at))""",
    "CREATE INDEX counters_time ON clan_counters(sampled_at)",
    """CREATE TABLE clan_membership (
        id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
        clan_id INTEGER NOT NULL REFERENCES clans(id), clan_role TEXT NOT NULL,
        from_at INTEGER NOT NULL, to_at INTEGER, previous_good_at INTEGER, end_previous_good_at INTEGER,
        last_observed_at INTEGER NOT NULL, end_basis TEXT,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        end_source_item_id INTEGER REFERENCES collection_items(id),
        CHECK(to_at IS NULL OR to_at >= from_at), UNIQUE(account_id,from_at))""",
    "CREATE UNIQUE INDEX clan_open ON clan_membership(account_id) WHERE to_at IS NULL",
    "CREATE INDEX clan_history ON clan_membership(clan_id,from_at,to_at)",
    """CREATE TABLE donation_snapshots (
        account_id INTEGER NOT NULL REFERENCES accounts(id), sampled_at INTEGER NOT NULL,
        clan_id INTEGER REFERENCES clans(id), season_key TEXT, donations INTEGER,
        received INTEGER, trophies INTEGER, builder_trophies INTEGER, clan_rank INTEGER,
        previous_rank INTEGER, field_presence_json TEXT NOT NULL,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        PRIMARY KEY(account_id,sampled_at))""",
    "CREATE INDEX donation_clan ON donation_snapshots(clan_id,sampled_at)",
)


def create_initial_schema(connection: sqlite3.Connection) -> None:
    for statement in STATEMENTS:
        connection.execute(statement)


PLAYER_STATEMENTS = (
    """CREATE TABLE player_observations (
        account_id INTEGER NOT NULL REFERENCES accounts(id), field_key TEXT NOT NULL,
        observed_at INTEGER NOT NULL, value_json TEXT NOT NULL, previous_good_at INTEGER,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        PRIMARY KEY(account_id,field_key,observed_at))""",
    "CREATE INDEX player_observation_time ON player_observations(account_id,observed_at)",
    """CREATE TABLE item_catalog (
        id INTEGER PRIMARY KEY, source_array TEXT NOT NULL, village TEXT NOT NULL,
        source_name TEXT NOT NULL, source_identifier TEXT, classification TEXT,
        definition_json TEXT NOT NULL, UNIQUE(source_array,village,source_name))""",
    """CREATE TABLE player_items (
        account_id INTEGER NOT NULL REFERENCES accounts(id), item_id INTEGER NOT NULL REFERENCES item_catalog(id),
        observed_at INTEGER NOT NULL, level INTEGER, max_level INTEGER,
        static_fields_json TEXT NOT NULL, present INTEGER NOT NULL CHECK(present IN (0,1)),
        previous_good_at INTEGER, source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        PRIMARY KEY(account_id,item_id,observed_at))""",
    "CREATE INDEX player_item_history ON player_items(item_id,observed_at)",
    """CREATE TABLE hero_equipment (
        account_id INTEGER NOT NULL REFERENCES accounts(id), hero_item_id INTEGER NOT NULL REFERENCES item_catalog(id),
        slot INTEGER NOT NULL, observed_at INTEGER NOT NULL,
        equipment_item_id INTEGER REFERENCES item_catalog(id), previous_good_at INTEGER,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        PRIMARY KEY(account_id,hero_item_id,slot,observed_at))""",
    "CREATE INDEX equipment_hero ON hero_equipment(hero_item_id,observed_at)",
    "CREATE INDEX equipment_item ON hero_equipment(equipment_item_id)",
    """CREATE TABLE player_counters (
        account_id INTEGER NOT NULL REFERENCES accounts(id), sampled_at INTEGER NOT NULL,
        trophies INTEGER, best_trophies INTEGER, builder_trophies INTEGER, best_builder_trophies INTEGER,
        war_stars INTEGER, attack_wins INTEGER, defense_wins INTEGER, capital_contributions INTEGER,
        donations INTEGER, received INTEGER, legend_counters_json TEXT,
        achievement_values_json TEXT NOT NULL, transient_item_fields_json TEXT NOT NULL,
        field_presence_json TEXT NOT NULL, reset_context_json TEXT NOT NULL,
        source_item_id INTEGER NOT NULL REFERENCES collection_items(id),
        PRIMARY KEY(account_id,sampled_at))""",
    "CREATE INDEX player_counter_time ON player_counters(sampled_at)",
)


def create_player_schema(connection: sqlite3.Connection) -> None:
    for statement in PLAYER_STATEMENTS:
        connection.execute(statement)


def compact_player_schema(connection: sqlite3.Connection) -> None:
    # Facts reference their run directly. Older item provenance is translated
    # before successful per-fetch receipts that have no remaining users go away.
    tables = ("player_observations", "player_items", "hero_equipment", "player_counters")
    for table in tables:
        statement = next(sql for sql in PLAYER_STATEMENTS if sql.startswith(f"CREATE TABLE {table} ("))
        replacement = statement.replace(f"CREATE TABLE {table} (", f"CREATE TABLE {table}_compact (")
        replacement = replacement.replace("source_item_id INTEGER NOT NULL REFERENCES collection_items(id)",
                                          "source_run_id INTEGER NOT NULL REFERENCES collection_runs(id)")
        connection.execute(replacement)
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        selected = ["i.run_id" if column == "source_item_id" else f"p.{column}" for column in columns]
        connection.execute(f"INSERT INTO {table}_compact SELECT {','.join(selected)} FROM {table} p "
                           "JOIN collection_items i ON i.id=p.source_item_id")
        connection.execute(f"DROP TABLE {table}")
        connection.execute(f"ALTER TABLE {table}_compact RENAME TO {table}")
    for statement in PLAYER_STATEMENTS:
        if statement.startswith("CREATE INDEX"):
            connection.execute(statement)
    connection.execute("""CREATE TABLE player_collection_state (
        account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
        next_due_at INTEGER, last_attempt_at INTEGER, last_status TEXT,
        active_run_id INTEGER REFERENCES collection_runs(id), ordinal INTEGER,
        queue_status TEXT, started_at INTEGER,
        last_good_json TEXT NOT NULL DEFAULT '{}', slow_sampled_at INTEGER,
        progress_sampled_at INTEGER, achievement_values_json TEXT NOT NULL DEFAULT '{}',
        counter_context_json TEXT NOT NULL DEFAULT '{}')""")
    connection.execute("CREATE INDEX player_active_run ON player_collection_state(active_run_id)")
    connection.execute("""CREATE TABLE player_coverage (
        account_id INTEGER NOT NULL REFERENCES accounts(id), from_at INTEGER NOT NULL,
        last_observed_at INTEGER NOT NULL, completeness_json TEXT NOT NULL,
        first_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        last_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        PRIMARY KEY(account_id,from_at))""")
    # Backfill operational checkpoints and the last full achievement map from
    # version 2. Sparse maps start only after this migration.
    from .player_state import migrate_state
    migrate_state(connection)
    connection.execute(
        "DELETE FROM collection_items WHERE source_kind='player_profile' AND status='ok' "
        "AND NOT EXISTS (SELECT 1 FROM account_aliases a WHERE a.source_item_id=collection_items.id "
        "OR a.end_source_item_id=collection_items.id)")


def create_schema(connection: sqlite3.Connection) -> None:
    create_initial_schema(connection)
    create_player_schema(connection)
    compact_player_schema(connection)


MIGRATIONS = (
    SQLiteMigration(1, "family people and membership", create_initial_schema),
    SQLiteMigration(2, "family player profiles", create_player_schema),
    SQLiteMigration(3, "compact player history", compact_player_schema),
)
