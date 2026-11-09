"""One version chain and secure connection policy for all agent repositories."""
from contextlib import contextmanager

from elbow_helper.infrastructure.persistence import SQLiteMigration, run_sqlite_migrations, sqlite_connection
from elbow_helper.infrastructure.persistence.versioned import validate_sqlite_shape

SCHEMA_VERSION = 9


def migrations():
    from elbow_helper.agent.conversation.repository import _create_schema as conversations
    from elbow_helper.agent.conversation.transcripts import _schema, _debug_schema
    from elbow_helper.agent.conversation.transcripts import _feedback_hint_schema
    from elbow_helper.agent.conversation.feedback import feedback_schema
    from elbow_helper.agent.research.repository import _create_schema as research, _add_job_kind
    from elbow_helper.agent.actions.schema import create_schema as actions
    from elbow_helper.agent.discord_actions.meme_usage import create_meme_usage
    steps = (("conversations", conversations), ("transcripts", _schema),
             ("diagnostics", _debug_schema), ("reply reports", feedback_schema),
             ("research", research), ("research kinds", _add_job_kind),
             ("actions and scheduling", actions), ("meme use", create_meme_usage),
             ("feedback hint count", _feedback_hint_schema))
    return tuple(SQLiteMigration(i, name, apply) for i, (name, apply) in enumerate(steps, 1))


def create_schema(connection):
    for step in migrations():
        step.apply(connection)


def initialize_agent(connection):
    run_sqlite_migrations(connection, migrations(), target_version=SCHEMA_VERSION)
    validate_sqlite_shape(connection, create_schema)


@contextmanager
def agent_connection(path, **kwargs):
    with sqlite_connection(path, **kwargs) as connection:
        connection.execute("PRAGMA secure_delete=ON")
        yield connection
