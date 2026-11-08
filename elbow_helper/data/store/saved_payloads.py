"""Immutable board evidence, addressed by its complete original payload."""

def create_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS saved_board_evidence (
        digest TEXT PRIMARY KEY, source TEXT NOT NULL, payload_json TEXT NOT NULL)""")
