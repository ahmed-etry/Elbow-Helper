"""Permanent visible chat archive, separate from expiring working snapshots."""

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3

from elbow_helper.infrastructure.persistence import SQLiteMigration, run_sqlite_migrations, sqlite_connection, sqlite_transaction

LOGGER = logging.getLogger(__name__)
DEBUG_RETENTION_DAYS = 30


def _debug_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE operator_debug_records (
        record_id INTEGER PRIMARY KEY, request_message_id INTEGER,
        guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
        root_message_id INTEGER, member_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, record_json TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX operator_debug_expiry ON operator_debug_records(created_at)")
    connection.execute("CREATE INDEX operator_debug_request ON operator_debug_records(request_message_id)")


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
        self._write_tasks: set[asyncio.Task] = set()
        self._write_lock = asyncio.Lock()
        with self.connect() as connection:
            run_sqlite_migrations(connection, (
                SQLiteMigration(1, "visible chat transcripts", _schema),
                SQLiteMigration(2, "operator request diagnostics", _debug_schema),
            ), target_version=2)

    def enqueue_debug(self, context, record) -> None:
        """Schedule best-effort diagnostics without waiting on SQLite or serialization."""
        self.enqueue_write(
            self.record_debug, record=record,
            request_message_id=getattr(context.source_message, "id", None),
            guild_id=context.guild.id, channel_id=context.source_message.channel.id,
            root_message_id=context.conversation_root_id, member_id=context.member.id,
        )

    def enqueue_debug_prune(self) -> None:
        self.enqueue_write(self.prune_debug)

    def enqueue_write(self, operation, **values) -> None:
        """Preserve request/reply order without putting archive I/O on delivery's path."""
        async def write():
            try:
                async with self._write_lock:
                    await archive_write(operation, **values)
            except Exception:
                LOGGER.exception("Agent transcript archive write failed")

        task = asyncio.create_task(write())
        self._write_tasks.add(task)
        task.add_done_callback(self._write_tasks.discard)

    async def drain(self) -> None:
        """Settle queued writes for operator exports and orderly shutdown."""
        while self._write_tasks:
            await asyncio.gather(*self._write_tasks)

    def record_debug(self, *, record, request_message_id, guild_id, channel_id,
                     root_message_id, member_id) -> None:
        now = datetime.now(timezone.utc)
        encoded = json.dumps(asdict(record), ensure_ascii=False, default=str)
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            connection.execute("""INSERT INTO operator_debug_records (
                request_message_id, guild_id, channel_id, root_message_id,
                member_id, created_at, record_json) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (request_message_id, guild_id, channel_id, root_message_id,
                 member_id, now.isoformat(), encoded))
            self._prune_debug(connection, now)

    @staticmethod
    def _prune_debug(connection, now):
        cutoff = (now - timedelta(days=DEBUG_RETENTION_DAYS)).isoformat()
        connection.execute("DELETE FROM operator_debug_records WHERE created_at < ?", (cutoff,))

    def prune_debug(self) -> None:
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            self._prune_debug(connection, datetime.now(timezone.utc))

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

    def replace_reply(
        self, *, message_id: int, request_message_id: int,
        previous_content: str, content: str,
    ) -> None:
        """Archive an edited notice only when its original identity still matches."""
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            row = connection.execute(
                "SELECT request_message_id, content FROM replies WHERE message_id=?", (message_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO replies(message_id, request_message_id, content) VALUES (?, ?, ?)",
                    (message_id, request_message_id, content),
                )
            elif tuple(row) == (request_message_id, content):
                return
            elif tuple(row) == (request_message_id, previous_content):
                connection.execute("UPDATE replies SET content=? WHERE message_id=?", (content, message_id))
            else:
                raise ValueError("Archived reply identity conflicts with the edited notice")

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
