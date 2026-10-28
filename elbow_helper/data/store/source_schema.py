"""Additive storage for rolling logs, finished seasons and reference sources."""

from .battles import create_battle_schema


def create_source_schema(connection):
    create_battle_schema(connection)
    connection.execute("""CREATE TABLE family_api_state (
        source_kind TEXT NOT NULL, scope_id TEXT NOT NULL, next_due_at INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, checkpoint_json TEXT NOT NULL DEFAULT '{}',
        last_attempt_at INTEGER, last_status TEXT, last_http_status INTEGER,
        last_run_id INTEGER REFERENCES collection_runs(id),
        active_run_id INTEGER REFERENCES collection_runs(id), ordinal INTEGER,
        queue_status TEXT, started_at INTEGER,
        PRIMARY KEY(source_kind,scope_id))""")
    connection.execute("CREATE INDEX family_api_active ON family_api_state(active_run_id)")
    connection.execute("""CREATE TABLE family_api_entities (
        source_kind TEXT NOT NULL, scope_id TEXT NOT NULL, entity_key TEXT NOT NULL,
        record_scope TEXT NOT NULL, source_at INTEGER, final_at INTEGER,
        first_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        PRIMARY KEY(source_kind,scope_id,entity_key)) WITHOUT ROWID""")
