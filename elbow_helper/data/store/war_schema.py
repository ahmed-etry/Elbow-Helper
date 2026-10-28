"""Additive competition storage; opponents remain local to their events.

Facts reference the collection run that last changed them, as player history
does. Successful polls leave no per-item receipt; only failures keep one.
"""


STATEMENTS = (
    """CREATE TABLE wars (
        id INTEGER PRIMARY KEY, type TEXT NOT NULL, war_tag TEXT UNIQUE,
        preparation_at INTEGER, start_at INTEGER, war_start_at INTEGER, end_at INTEGER NOT NULL,
        state TEXT NOT NULL, team_size INTEGER, attacks_per_member INTEGER,
        battle_modifier_json TEXT, first_seen_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
        final_at INTEGER, roster_complete INTEGER NOT NULL, attacks_complete INTEGER NOT NULL,
        result_complete INTEGER NOT NULL, privacy_redacted INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT NOT NULL, field_presence_json TEXT NOT NULL,
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id))""",
    "CREATE INDEX wars_type_end ON wars(type,end_at)",
    "CREATE INDEX wars_state_end ON wars(state,end_at)",
    """CREATE TABLE war_source_keys (
        source_kind TEXT NOT NULL, source_key TEXT NOT NULL, war_id INTEGER NOT NULL REFERENCES wars(id),
        PRIMARY KEY(source_kind,source_key))""",
    "CREATE INDEX war_keys_event ON war_source_keys(war_id)",
    """CREATE TABLE war_sides (
        war_id INTEGER NOT NULL REFERENCES wars(id), side INTEGER NOT NULL, clan_tag TEXT NOT NULL,
        name TEXT, level INTEGER, badge_json TEXT, stars INTEGER, destruction REAL, attacks INTEGER,
        xp_earned INTEGER, result TEXT, extra_json TEXT NOT NULL,
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id), PRIMARY KEY(war_id,side))""",
    "CREATE INDEX war_sides_clan ON war_sides(clan_tag,war_id)",
    # The war log's CWL entry totals the whole league week. Its opponent is
    # an unnamed aggregate, so it cannot share a two-clan war identity.
    """CREATE TABLE cwl_log_summaries (
        clan_tag TEXT NOT NULL, end_at INTEGER NOT NULL, team_size INTEGER NOT NULL,
        attacks_per_member INTEGER NOT NULL, result TEXT, battle_modifier_json TEXT,
        name TEXT, level INTEGER, badge_json TEXT, attacks INTEGER, stars INTEGER,
        destruction REAL, xp_earned INTEGER, opponent_level INTEGER,
        opponent_badge_json TEXT, opponent_stars INTEGER, opponent_destruction REAL,
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        PRIMARY KEY(clan_tag,end_at))""",
    """CREATE TABLE war_members (
        id INTEGER PRIMARY KEY, war_id INTEGER NOT NULL REFERENCES wars(id), side INTEGER NOT NULL,
        player_tag TEXT NOT NULL, account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
        name TEXT, townhall INTEGER, source_map_position INTEGER, attacks_expected INTEGER,
        attacks_used INTEGER, opponent_attacks INTEGER, extra_json TEXT NOT NULL,
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        UNIQUE(war_id,side,player_tag), FOREIGN KEY(war_id,side) REFERENCES war_sides(war_id,side))""",
    "CREATE INDEX war_members_account ON war_members(account_id,war_id)",
    """CREATE TABLE war_attacks (
        war_id INTEGER NOT NULL REFERENCES wars(id), attack_order INTEGER NOT NULL,
        attacker_member_id INTEGER REFERENCES war_members(id) ON DELETE SET NULL,
        defender_member_id INTEGER REFERENCES war_members(id) ON DELETE SET NULL,
        stars INTEGER, destruction REAL, duration INTEGER, extra_json TEXT NOT NULL,
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id), PRIMARY KEY(war_id,attack_order))""",
    "CREATE INDEX attacks_attacker ON war_attacks(attacker_member_id,war_id)",
    "CREATE INDEX attacks_defender ON war_attacks(defender_member_id,war_id)",
    """CREATE TABLE cwl_seasons (
        id INTEGER PRIMARY KEY, season_key TEXT NOT NULL, group_key TEXT NOT NULL, state TEXT NOT NULL,
        league_id INTEGER, league_name TEXT, first_seen_at INTEGER NOT NULL, final_at INTEGER,
        group_metadata_json TEXT NOT NULL, source_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        UNIQUE(season_key,group_key))""",
    """CREATE TABLE cwl_clans (
        season_id INTEGER NOT NULL REFERENCES cwl_seasons(id), clan_tag TEXT NOT NULL,
        name TEXT, level INTEGER, badge_json TEXT, extra_json TEXT NOT NULL,
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id), PRIMARY KEY(season_id,clan_tag))""",
    """CREATE TABLE cwl_players (
        season_id INTEGER NOT NULL REFERENCES cwl_seasons(id), clan_tag TEXT NOT NULL,
        player_tag TEXT NOT NULL, account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
        name TEXT, townhall INTEGER, extra_json TEXT NOT NULL,
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id), PRIMARY KEY(season_id,clan_tag,player_tag))""",
    "CREATE INDEX cwl_players_account ON cwl_players(account_id,season_id)",
    """CREATE TABLE cwl_rounds (
        season_id INTEGER NOT NULL REFERENCES cwl_seasons(id), round_no INTEGER NOT NULL,
        war_tag TEXT NOT NULL, war_id INTEGER REFERENCES wars(id),
        source_run_id INTEGER NOT NULL REFERENCES collection_runs(id), PRIMARY KEY(season_id,round_no,war_tag))""",
    "CREATE INDEX cwl_round_tag ON cwl_rounds(war_tag)",
    """CREATE TABLE cwl_season_sources (
        season_id INTEGER NOT NULL REFERENCES cwl_seasons(id), clan_tag TEXT NOT NULL,
        PRIMARY KEY(season_id,clan_tag))""",
    # Each source's schedule doubles as the run queue, like player checkpoints.
    """CREATE TABLE war_collection_state (
        source_kind TEXT NOT NULL, scope_id TEXT NOT NULL, next_due_at INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, war_id INTEGER REFERENCES wars(id),
        checkpoint_json TEXT NOT NULL DEFAULT '{}',
        active_run_id INTEGER REFERENCES collection_runs(id), ordinal INTEGER,
        queue_status TEXT, started_at INTEGER, PRIMARY KEY(source_kind,scope_id))""",
    "CREATE INDEX war_collection_due ON war_collection_state(enabled,next_due_at)",
    "CREATE INDEX war_active_run ON war_collection_state(active_run_id,ordinal)",
    """CREATE VIEW cwl_attacks AS SELECT r.season_id,r.round_no,a.*
        FROM cwl_rounds r JOIN wars w ON w.id=r.war_id JOIN war_attacks a ON a.war_id=w.id
        WHERE w.type='CWL'""",
)


def create_war_schema(connection):
    for statement in STATEMENTS:
        connection.execute(statement)
