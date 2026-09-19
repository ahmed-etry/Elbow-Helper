"""Permanent visible chat archive, separate from expiring working snapshots."""

import asyncio
from pathlib import Path
import sqlite3

from elbow_helper.infrastructure.persistence import SQLiteMigration, run_sqlite_migrations, sqlite_connection, sqlite_transaction


def _schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE requests (
        message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL, root_message_id INTEGER NOT NULL,
        member_id INTEGER NOT NULL, created_at TEXT NOT NULL,
        content TEXT NOT NULL, replied_to_message_id INTEGER
    )""")
    connection.execute("""CREATE TABLE replies (
        message_id INTEGER PRIMARY KEY, request_message_id INTEGER NOT NULL
        REFERENCES requests(message_id), content TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    connection.execute("CREATE INDEX transcript_conversation ON requests(guild_id, channel_id, root_message_id, message_id)")
    connection.execute("CREATE INDEX transcript_replies ON replies(request_message_id, message_id)")


class TranscriptArchive:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as connection:
            run_sqlite_migrations(connection, (SQLiteMigration(1, "visible chat transcripts", _schema),), target_version=1)

    def connect(self):
        return sqlite_connection(self.path, synchronous="FULL", busy_timeout_ms=5000)

    def record_request(self, *, message_id: int, guild_id: int, channel_id: int,
                       root_message_id: int, member_id: int, created_at: str,
                       content: str, replied_to_message_id: int | None) -> None:
        values = (message_id, guild_id, channel_id, root_message_id, member_id,
                  created_at, content, replied_to_message_id)
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            row = connection.execute("SELECT * FROM requests WHERE message_id=?", (message_id,)).fetchone()
            if row is not None:
                if tuple(row) != values:
                    raise ValueError("Archived request identity conflicts with existing content")
                return
            connection.execute("INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values)

    def record_reply(self, *, message_id: int, request_message_id: int, content: str) -> None:
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            row = connection.execute("SELECT request_message_id, content FROM replies WHERE message_id=?", (message_id,)).fetchone()
            if row is not None:
                if tuple(row) != (request_message_id, content):
                    raise ValueError("Archived reply identity conflicts with existing content")
                return
            connection.execute("INSERT INTO replies(message_id, request_message_id, content) VALUES (?, ?, ?)",
                               (message_id, request_message_id, content))

    def read_page(self, *, guild_id: int, channel_id: int, root_message_id: int,
                  after_message_id: int = 0, limit: int = 50) -> tuple[dict, ...]:
        """Operator-side structured export; never an unrestricted agent tool."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Transcript page limit must be between 1 and 100")
        with self.connect() as connection, sqlite_transaction(connection):
            requests = connection.execute("""SELECT * FROM requests
                WHERE guild_id=? AND channel_id=? AND root_message_id=? AND message_id>?
                ORDER BY message_id LIMIT ?""", (guild_id, channel_id, root_message_id, after_message_id, limit)).fetchall()
            result = []
            for request in requests:
                item = dict(request)
                item["replies"] = [dict(row) for row in connection.execute(
                    "SELECT message_id, content, created_at FROM replies WHERE request_message_id=? ORDER BY message_id",
                    (request["message_id"],))]
                result.append(item)
        return tuple(result)


async def archive_write(operation, **values) -> None:
    """Do not abandon a started SQLite write during handler cancellation."""
    task = asyncio.create_task(asyncio.to_thread(operation, **values))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
