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


EVIDENCE_STATEMENTS = (
    "ALTER TABLE wars ADD COLUMN type_basis TEXT",
    # Every check updates its source's status, even when no war fact changes.
    "ALTER TABLE war_collection_state ADD COLUMN last_attempt_at INTEGER",
    "ALTER TABLE war_collection_state ADD COLUMN last_success_at INTEGER",
    "ALTER TABLE war_collection_state ADD COLUMN last_outcome TEXT",
    "ALTER TABLE war_collection_state ADD COLUMN last_http_status INTEGER",
    "ALTER TABLE war_collection_state ADD COLUMN last_run_id INTEGER REFERENCES collection_runs(id)",
    "ALTER TABLE war_collection_state ADD COLUMN observed_json TEXT NOT NULL DEFAULT '{}'",
    # Old clan health war IDs and war cache entries, each mapped once to the
    # record's war with what the old store said about it at capture.
    """CREATE TABLE war_legacy_ids (
        source TEXT NOT NULL CHECK(source IN ('clan_health','war_cache')),
        legacy_id TEXT NOT NULL, clan_code TEXT NOT NULL,
        war_id INTEGER NOT NULL REFERENCES wars(id),
        legacy_label TEXT, capture_state TEXT, captured_at INTEGER, mapped_at INTEGER NOT NULL,
        PRIMARY KEY(source,legacy_id,clan_code))""",
    "CREATE INDEX war_legacy_war ON war_legacy_ids(war_id)",
)


def merge_log_doubles(connection):
    """Fold each log-only copy into its current-war copy; return how many merged.

    Before end-drift matching, a war-log entry whose end was a second or two
    later became a second war. Only a unique untagged partner with a
    preparation time qualifies, so two current-war copies never merge.
    """
    from .wars import END_DRIFT
    merged = 0
    for log_id, end in connection.execute(
            "SELECT id,end_at FROM wars WHERE preparation_at IS NULL AND war_tag IS NULL").fetchall():
        sides = connection.execute("SELECT clan_tag FROM war_sides WHERE war_id=? ORDER BY side", (log_id,)).fetchall()
        if len(sides) != 2:
            continue
        partners = connection.execute(
            "SELECT w.id FROM wars w JOIN war_sides a ON a.war_id=w.id AND a.side=0 AND a.clan_tag=? "
            "JOIN war_sides b ON b.war_id=w.id AND b.side=1 AND b.clan_tag=? "
            "WHERE w.preparation_at IS NOT NULL AND w.war_tag IS NULL AND w.end_at BETWEEN ? AND ?",
            (sides[0][0], sides[1][0], end - END_DRIFT, end + END_DRIFT)).fetchall()
        if len(partners) != 1:
            continue
        war_id = partners[0][0]
        # The current-war copy keeps its own facts and end; the log adds what it lacks, such as the result.
        for column in ("name", "level", "badge_json", "stars", "destruction", "attacks", "xp_earned", "result"):
            connection.execute(
                f"UPDATE war_sides SET {column}=(SELECT l.{column} FROM war_sides l WHERE l.war_id=? AND l.side=war_sides.side) "
                f"WHERE war_id=? AND {column} IS NULL", (log_id, war_id))
        connection.execute("UPDATE war_source_keys SET war_id=? WHERE war_id=?", (war_id, log_id))
        connection.execute("UPDATE war_collection_state SET war_id=? WHERE war_id=?", (war_id, log_id))
        connection.execute("DELETE FROM war_sides WHERE war_id=?", (log_id,))
        connection.execute("DELETE FROM wars WHERE id=?", (log_id,))
        merged += 1
    return merged


def extend_war_schema(connection):
    for statement in EVIDENCE_STATEMENTS:
        connection.execute(statement)
    merge_log_doubles(connection)
    from .war_projection import war_type
    for war_id, war_tag, preparation, start, end in connection.execute(
            "SELECT id,war_tag,preparation_at,start_at,end_at FROM wars").fetchall():
        logged = preparation is None or connection.execute(
            "SELECT 1 FROM war_source_keys WHERE war_id=? AND source_kind='war_log'", (war_id,)).fetchone()
        connection.execute("UPDATE wars SET type=?,type_basis=? WHERE id=?",
                           (*war_type(preparation, start, end, war_tag=war_tag, logged=bool(logged)), war_id))
    # Which log a stored entry came from was not kept, so absence cannot be
    # judged here. Reading each public log again settles it on the next pass.
    connection.execute("UPDATE war_collection_state SET next_due_at=0 WHERE source_kind='war_log'")
