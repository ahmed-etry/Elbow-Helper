"""Versioned agent-owned conversation snapshots and reply identities."""

from dataclasses import dataclass
from contextlib import contextmanager
import math
from pathlib import Path
import sqlite3

from elbow_helper.infrastructure.persistence import (
    SQLiteMigration, run_sqlite_migrations, sqlite_connection, sqlite_transaction,
)
from .state import MAX_CONVERSATIONS, MAX_REPLY_REFERENCES


MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


class SnapshotConflict(RuntimeError):
    """A stale writer must not overwrite a newer conversation snapshot."""


class SnapshotCapacityError(RuntimeError):
    """Protected active snapshots cannot be evicted to admit another write."""


@dataclass(frozen=True, slots=True)
class StoredConversation:
    guild_id: int
    channel_id: int
    root_message_id: int
    revision: int
    touched_at: float
    expires_at: float
    payload: str


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE conversations (
        guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, root_message_id INTEGER NOT NULL,
        revision INTEGER NOT NULL, touched_at REAL NOT NULL, expires_at REAL NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY (guild_id, channel_id, root_message_id)
    )""")
    connection.execute("""CREATE TABLE conversation_replies (
        guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
        root_message_id INTEGER NOT NULL, PRIMARY KEY (guild_id, channel_id, message_id),
        FOREIGN KEY (guild_id, channel_id, root_message_id)
        REFERENCES conversations(guild_id, channel_id, root_message_id) ON DELETE CASCADE
    )""")
    connection.execute("CREATE INDEX conversations_expiry ON conversations(expires_at)")


class ConversationRepository:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as connection:
            run_sqlite_migrations(connection, (SQLiteMigration(1, "conversation snapshots", _create_schema),), target_version=1)
            # Fail startup for a damaged declared schema instead of losing history silently.
            connection.execute("SELECT guild_id, channel_id, root_message_id, revision, touched_at, expires_at, payload FROM conversations LIMIT 0")
            connection.execute("SELECT guild_id, channel_id, message_id, root_message_id FROM conversation_replies LIMIT 0")

    @contextmanager
    def connect(self):
        with sqlite_connection(self.path, synchronous="FULL", busy_timeout_ms=5000) as connection:
            connection.execute("PRAGMA secure_delete=ON")
            yield connection

    def save(self, snapshot: StoredConversation, reply_ids: tuple[int, ...], *, protected_roots: tuple[int, ...] = ()) -> int:
        if (any(type(value) is not int or value <= 0 for value in
                (snapshot.guild_id, snapshot.channel_id, snapshot.root_message_id, *reply_ids))
                or type(snapshot.revision) is not int or snapshot.revision < 0):
            raise ValueError("Invalid conversation identity or revision")
        if len(snapshot.payload.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Conversation snapshot exceeds its storage budget")
        if not all(math.isfinite(value) for value in (snapshot.expires_at, snapshot.touched_at)) or snapshot.expires_at <= snapshot.touched_at:
            raise ValueError("Conversation expiry must follow its last activity")
        if len(reply_ids) != len(set(reply_ids)):
            raise ValueError("Duplicate conversation reply identities")
        if len(reply_ids) > MAX_REPLY_REFERENCES:
            raise ValueError("Too many retained conversation replies")
        key = (snapshot.guild_id, snapshot.channel_id, snapshot.root_message_id)
        with self.connect() as connection, sqlite_transaction(connection, immediate=True):
            row = connection.execute(
                "SELECT revision FROM conversations WHERE guild_id=? AND channel_id=? AND root_message_id=?", key,
            ).fetchone()
            if (int(row["revision"]) if row else 0) != snapshot.revision:
                raise SnapshotConflict("Conversation snapshot revision changed")
            revision = snapshot.revision + 1
            connection.execute("""INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, channel_id, root_message_id) DO UPDATE SET
                revision=excluded.revision, touched_at=excluded.touched_at,
                expires_at=excluded.expires_at, payload=excluded.payload""",
                (*key, revision, snapshot.touched_at, snapshot.expires_at, snapshot.payload))
            connection.execute("DELETE FROM conversation_replies WHERE guild_id=? AND channel_id=? AND root_message_id=?", key)
            connection.executemany("INSERT INTO conversation_replies VALUES (?, ?, ?, ?)",
                ((snapshot.guild_id, snapshot.channel_id, message_id, snapshot.root_message_id) for message_id in reply_ids))
            excess = int(connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]) - MAX_CONVERSATIONS
            if excess > 0:
                protected = tuple(dict.fromkeys((*protected_roots, snapshot.root_message_id)))
                placeholders = ",".join("?" for _ in protected)
                victims = connection.execute(
                    f"SELECT guild_id, channel_id, root_message_id FROM conversations WHERE root_message_id NOT IN ({placeholders}) "
                    "ORDER BY touched_at, root_message_id LIMIT ?", (*protected, excess),
                ).fetchall()
                if len(victims) < excess:
                    raise SnapshotCapacityError("All eligible conversation snapshots are protected")
                connection.executemany("DELETE FROM conversations WHERE guild_id=? AND channel_id=? AND root_message_id=?",
                                       (tuple(row) for row in victims))
        return revision

    def load_active(self, *, now: float, limit: int) -> tuple[StoredConversation, ...]:
        if type(limit) is not int or not 1 <= limit <= MAX_CONVERSATIONS:
            raise ValueError("Invalid conversation load limit")
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM conversations WHERE expires_at > ? ORDER BY touched_at DESC, root_message_id DESC LIMIT ?",
                                      (now, limit)).fetchall()
        return tuple(StoredConversation(**dict(row)) for row in reversed(rows))

    def find_reply(self, guild_id: int, channel_id: int, message_id: int, *, now: float) -> StoredConversation | None:
        with self.connect() as connection:
            row = connection.execute("""SELECT c.* FROM conversation_replies r JOIN conversations c
                USING (guild_id, channel_id, root_message_id)
                WHERE r.guild_id=? AND r.channel_id=? AND r.message_id=? AND c.expires_at > ?""",
                (guild_id, channel_id, message_id, now)).fetchone()
        return StoredConversation(**dict(row)) if row else None

    def prune(self, *, now: float, protected_roots: tuple[int, ...] = ()) -> int:
        with self.connect() as connection:
            with sqlite_transaction(connection, immediate=True):
                protected = tuple(dict.fromkeys(protected_roots)) or (0,)
                placeholders = ",".join("?" for _ in protected)
                return connection.execute(f"DELETE FROM conversations WHERE expires_at <= ? AND root_message_id NOT IN ({placeholders})",
                                          (now, *protected)).rowcount

    def delete(self, guild_id: int, channel_id: int, root_message_id: int) -> None:
        with self.connect() as connection:
            with sqlite_transaction(connection, immediate=True):
                connection.execute("DELETE FROM conversations WHERE guild_id=? AND channel_id=? AND root_message_id=?",
                                   (guild_id, channel_id, root_message_id))
