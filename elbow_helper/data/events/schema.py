"""Workflow history and live state share a versioned SQLite file."""

from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration
from .settings import create_schema as create_settings
from elbow_helper.infrastructure.persistence.documents import create_schema as create_documents


SCHEMA_VERSION = 4


def _create_history(connection):
    connection.execute("""CREATE TABLE workflow_events (
        id INTEGER PRIMARY KEY, workflow TEXT NOT NULL, case_key TEXT NOT NULL,
        kind TEXT NOT NULL, occurred_at INTEGER NOT NULL, recorded_at INTEGER NOT NULL,
        subject_discord_id INTEGER, subject_tag TEXT, clan_code TEXT,
        actor_kind TEXT NOT NULL, actor_discord_id INTEGER, via TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}', source_channel_id INTEGER,
        source_message_id INTEGER, idem_key TEXT NOT NULL UNIQUE)""")
    connection.execute("CREATE INDEX events_subject ON workflow_events(subject_discord_id,occurred_at)")
    connection.execute("CREATE INDEX events_actor ON workflow_events(actor_discord_id)")
    connection.execute("CREATE INDEX events_kind ON workflow_events(workflow,kind,occurred_at)")
    connection.execute("CREATE INDEX events_case ON workflow_events(workflow,case_key,occurred_at)")
    # A row also marks the completed startup import, including an empty import.
    connection.execute("""CREATE TABLE workflow_state (
        workflow TEXT PRIMARY KEY, state_json TEXT NOT NULL, imported_at INTEGER NOT NULL)""")


def _create_examination_cases(connection):
    # One row per open examination case, so a change saves only its own case.
    connection.execute("""CREATE TABLE examination_cases (
        ticket_channel_id INTEGER PRIMARY KEY, case_json TEXT NOT NULL, updated_at INTEGER NOT NULL)""")


def create_schema(connection):
    _create_history(connection)
    _create_examination_cases(connection)
    create_settings(connection)
    create_documents(connection)


MIGRATIONS = (
    SQLiteMigration(1, "workflow history and state", _create_history),
    SQLiteMigration(2, "examination cases", _create_examination_cases),
    SQLiteMigration(3, "health settings and audit", create_settings),
    SQLiteMigration(4, "feature documents and startup receipt", create_documents),
)
