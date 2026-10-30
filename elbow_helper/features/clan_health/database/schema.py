"""Version-one schema and connection boundary."""

from __future__ import annotations

import sqlite3

from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration
from elbow_helper.infrastructure.persistence.versioned import (
    VersionedSQLiteDatabase, validate_sqlite_shape,
)

from .migrations import deduplicate_regular_wars, key_stored_cwl_seasons


def create_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS player_snapshots (
            captured_ts INTEGER NOT NULL,
            clan_code TEXT NOT NULL,
            player_tag TEXT NOT NULL,
            player_name TEXT NOT NULL,
            townhall INTEGER,
            donations INTEGER,
            donations_received INTEGER,
            trophies INTEGER,
            war_stars INTEGER,
            attack_wins INTEGER,
            capital_contrib INTEGER,
            hero_sum INTEGER,
            pet_sum INTEGER DEFAULT 0,
            equipment_sum INTEGER DEFAULT 0,
            troop_sum INTEGER DEFAULT 0,
            spell_sum INTEGER DEFAULT 0,
            games_total INTEGER,
            PRIMARY KEY (captured_ts, clan_code, player_tag)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS report_runs (
            run_id TEXT PRIMARY KEY,
            created_ts INTEGER NOT NULL,
            season_key TEXT NOT NULL,
            scope TEXT NOT NULL,
            partial INTEGER NOT NULL,
            cycle_start_ts INTEGER NOT NULL,
            cycle_end_ts INTEGER NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS report_players (
            run_id TEXT NOT NULL,
            season_key TEXT NOT NULL,
            clan_code TEXT NOT NULL,
            player_tag TEXT NOT NULL,
            player_name TEXT NOT NULL,
            status TEXT NOT NULL,
            flags_json TEXT NOT NULL,
            note TEXT NOT NULL,
            war_hits_used INTEGER DEFAULT 0,
            war_hits_expected INTEGER DEFAULT 0,
            war_missed INTEGER DEFAULT 0,
            war_stars_total REAL DEFAULT 0,
            war_destruction_total REAL DEFAULT 0,
            war_attack_count INTEGER DEFAULT 0,
            raid_attacks INTEGER DEFAULT 0,
            raid_expected INTEGER DEFAULT 0,
            raid_loot INTEGER DEFAULT 0,
            raid_expected_estimated INTEGER DEFAULT 0,
            donations INTEGER DEFAULT 0,
            donations_received INTEGER DEFAULT 0,
            trophies INTEGER DEFAULT 0,
            war_stars INTEGER DEFAULT 0,
            attack_wins INTEGER DEFAULT 0,
            capital_contrib INTEGER DEFAULT 0,
            townhall INTEGER DEFAULT 0,
            hero_sum INTEGER DEFAULT 0,
            pet_sum INTEGER DEFAULT 0,
            equipment_sum INTEGER DEFAULT 0,
            troop_sum INTEGER DEFAULT 0,
            spell_sum INTEGER DEFAULT 0,
            games_total INTEGER DEFAULT 0,
            hero_delta INTEGER,
            pet_delta INTEGER,
            equipment_delta INTEGER,
            troop_delta INTEGER,
            spell_delta INTEGER,
            capital_delta INTEGER,
            th_delta INTEGER,
            games_delta INTEGER,
            PRIMARY KEY (run_id, clan_code, player_tag),
            FOREIGN KEY (run_id) REFERENCES report_runs(run_id) ON DELETE CASCADE
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS war_activity (
            war_id TEXT NOT NULL,
            war_type TEXT NOT NULL,
            clan_code TEXT NOT NULL,
            clan_tag TEXT NOT NULL,
            end_ts INTEGER NOT NULL,
            player_tag TEXT NOT NULL,
            player_name TEXT NOT NULL,
            attacks_expected INTEGER DEFAULT 0,
            attacks_used INTEGER DEFAULT 0,
            stars REAL DEFAULT 0,
            destruction REAL DEFAULT 0,
            attack_count INTEGER DEFAULT 0,
            source TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (war_id, clan_code, player_tag)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS player_directory (
            player_tag TEXT PRIMARY KEY,
            player_name TEXT NOT NULL,
            clan_code TEXT NOT NULL,
            townhall INTEGER DEFAULT 0,
            player_name_search TEXT NOT NULL,
            player_tag_search TEXT NOT NULL,
            clan_code_search TEXT NOT NULL,
            first_seen_ts INTEGER NOT NULL,
            last_seen_ts INTEGER NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS wars (
            war_id TEXT NOT NULL,
            war_type TEXT NOT NULL,
            clan_code TEXT NOT NULL,
            clan_tag TEXT NOT NULL,
            opponent_tag TEXT NOT NULL DEFAULT '',
            opponent_name TEXT NOT NULL DEFAULT '',
            cwl_season TEXT NOT NULL DEFAULT '',
            cwl_season_label TEXT NOT NULL DEFAULT '',
            cwl_league TEXT NOT NULL DEFAULT '',
            cwl_round INTEGER DEFAULT 0,
            team_size INTEGER DEFAULT 0,
            attacks_per_member INTEGER DEFAULT 0,
            state TEXT NOT NULL DEFAULT '',
            preparation_start_ts INTEGER DEFAULT 0,
            start_ts INTEGER DEFAULT 0,
            end_ts INTEGER DEFAULT 0,
            last_seen_ts INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (war_id, clan_code)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS war_roster_members (
            war_id TEXT NOT NULL,
            clan_code TEXT NOT NULL,
            player_tag TEXT NOT NULL,
            player_name TEXT NOT NULL,
            townhall INTEGER DEFAULT 0,
            map_position INTEGER DEFAULT 0,
            attacks_expected INTEGER DEFAULT 0,
            attacks_used INTEGER DEFAULT 0,
            roster_state TEXT NOT NULL,
            captured_ts INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (war_id, clan_code, player_tag)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS war_attacks (
            war_id TEXT NOT NULL,
            war_type TEXT NOT NULL,
            clan_code TEXT NOT NULL,
            clan_tag TEXT NOT NULL,
            end_ts INTEGER NOT NULL,
            war_state TEXT NOT NULL,
            player_tag TEXT NOT NULL,
            player_name TEXT NOT NULL,
            attack_order INTEGER NOT NULL,
            defender_tag TEXT DEFAULT '',
            defender_name TEXT DEFAULT '',
            defender_map_position INTEGER DEFAULT 0,
            defender_townhall INTEGER DEFAULT 0,
            stars INTEGER DEFAULT 0,
            destruction REAL DEFAULT 0,
            fresh_attack INTEGER DEFAULT 0,
            duration INTEGER DEFAULT 0,
            source TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (war_id, clan_code, player_tag, attack_order, defender_tag)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS raid_member_activity (
            weekend_id TEXT NOT NULL,
            clan_code TEXT NOT NULL,
            clan_tag TEXT NOT NULL,
            end_ts INTEGER NOT NULL,
            player_tag TEXT NOT NULL,
            player_name TEXT NOT NULL,
            attacks INTEGER DEFAULT 0,
            attack_limit INTEGER DEFAULT 0,
            bonus_attack_limit INTEGER DEFAULT 0,
            attacks_expected INTEGER DEFAULT 0,
            loot INTEGER DEFAULT 0,
            source TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (weekend_id, clan_code, player_tag)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS clan_player_health_config (
            clan_code TEXT PRIMARY KEY,
            seed_template TEXT NOT NULL,
            seeded_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS clan_health_config_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            clan_code TEXT NOT NULL,
            layer TEXT NOT NULL,
            block TEXT NOT NULL,
            key TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            actor_discord_id INTEGER NOT NULL,
            actor_display TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_player_snapshots_tag_ts ON player_snapshots(player_tag, captured_ts)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_player_directory_name ON player_directory(player_name_search)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_player_directory_tag ON player_directory(player_tag_search)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_player_directory_clan ON player_directory(clan_code_search)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_player_directory_recent ON player_directory(last_seen_ts DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_report_players_season_tag ON report_players(season_key, player_tag)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_report_runs_membership "
        "ON report_runs(scope, partial, created_ts DESC, run_id DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_war_activity_player_ts ON war_activity(player_tag, end_ts)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_war_activity_clan_ts ON war_activity(clan_code, end_ts)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_war_activity_end_ts ON war_activity(end_ts)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_wars_clan_end_ts ON wars(clan_code, end_ts)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_wars_cwl_season ON wars(cwl_season, clan_code, cwl_round)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_war_roster_player ON war_roster_members(player_tag, war_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_war_attacks_player_ts ON war_attacks(player_tag, end_ts)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_war_attacks_war ON war_attacks(war_id, clan_code)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_raid_member_player_ts ON raid_member_activity(player_tag, end_ts)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_raid_member_weekend ON raid_member_activity(weekend_id, clan_code)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_chca_clan_ts ON clan_health_config_audit(clan_code, ts_utc DESC)"
    )
    key_stored_cwl_seasons(conn)


_NULLABLE = {
    "player_snapshots": {"townhall", "war_stars", "attack_wins", "capital_contrib", "hero_sum",
                         "pet_sum", "equipment_sum", "troop_sum", "spell_sum", "games_total"},
    "report_players": {"townhall", "war_stars", "attack_wins", "capital_contrib", "hero_sum",
                       "pet_sum", "equipment_sum", "troop_sum", "spell_sum", "games_total",
                       "hero_delta", "pet_delta", "equipment_delta", "troop_delta", "spell_delta",
                       "capital_delta", "th_delta", "games_delta"},
    "clan_health_config_audit": {"old_value"},
}


def validate_schema(conn: sqlite3.Connection, *, warn: bool = True, legacy: bool = False) -> None:
    # Every INSERT names all fact/config columns (audit alone omits id); SELECT *
    # becomes a dict. FK cascades are not used: connections leave foreign_keys OFF.
    # Rosters delete each war's rows before inserting; audit IDs are never read.
    # Neither table uses its key for conflict handling or row identity lookups.
    validate_sqlite_shape(
        conn, create_schema,
        missing_columns={"wars": {"cwl_season_label"}} if legacy else None,
        required_nullable=_NULLABLE,
        optional_keys=frozenset({"war_roster_members", "clan_health_config_audit"}),
        omitted_columns={"clan_health_config_audit": {"id"}}, warn=warn,
    )


def adopt_schema(conn: sqlite3.Connection) -> None:
    validate_schema(conn, warn=False, legacy=True)
    key_stored_cwl_seasons(conn)
    validate_schema(conn, warn=False)


SCHEMA_VERSION = 2

DATABASE = VersionedSQLiteDatabase(
    "clan health", create_schema, adopt_schema,
    migrations=(
        SQLiteMigration(1, "clan health baseline", create_schema),
        SQLiteMigration(2, "deduplicate delayed regular wars", deduplicate_regular_wars),
    ),
    target_version=SCHEMA_VERSION, validate=validate_schema,
)


class ClanHealthSchema:
    def _init_db(self) -> None:
        DATABASE.initialize(self.path)

    def _db_connect(self, **kwargs) -> sqlite3.Connection:
        return DATABASE.connect(self.path, **kwargs)
