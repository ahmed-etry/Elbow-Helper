"""Versioned, additive family-record schema. Times are UTC microseconds."""

import sqlite3

from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration


# Later subjects add migrations; they must not rewrite this baseline.
SCHEMA_VERSION = 1
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


def create_schema(connection: sqlite3.Connection) -> None:
    for statement in STATEMENTS:
        connection.execute(statement)


MIGRATIONS = (SQLiteMigration(1, "family people and membership", create_schema),)
