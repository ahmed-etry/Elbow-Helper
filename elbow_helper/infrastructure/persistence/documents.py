"""Feature documents in SQLite. Call these blocking operations on a worker.

Explicit filesystem paths are also accepted for isolated feature fixtures.
Runtime references always name a document in the workflows database.
"""
from dataclasses import dataclass
from contextlib import contextmanager, ExitStack
from functools import wraps
from threading import RLock
from hashlib import sha256
import asyncio
import json
from pathlib import Path

from .json_store import read_json, write_json_atomic
from .sqlite import sqlite_connection, sqlite_transaction

NAMES = ("clan_reporting", "wars", "cwl_threads", "cwl_reminders", "cwl_transfers",
         "cwl_dashboards", "cwl_routing", "cwl_bonus_rules", "cwl_bonus_previous",
         "cwl_bonus_audit", "cwl_bonus_boards", "activity_schedule", "role_connections",
         "application")
EVIDENCE_FIELDS = {"wars": ("war_board_history",),
                   "cwl_routing": ("rosters", "roster_names")}


_WRITERS = {}
_WRITERS_LOCK = RLock()


def serialized_mutation(operation):
    """Keep a synchronous read/change/save indivisible across worker callers."""
    @wraps(operation)
    def serialized(self, *args, **kwargs):
        with _WRITERS_LOCK:
            lock = self.__dict__.setdefault("_document_mutation_lock", RLock())
        with lock:
            return operation(self, *args, **kwargs)
    return serialized


def create_schema(connection):
    allowed = ",".join(repr(name) for name in NAMES)
    connection.execute(f"""CREATE TABLE feature_documents (
        name TEXT PRIMARY KEY CHECK(name IN ({allowed})), payload_json TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE storage_move_receipt (
        id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
        details_json TEXT NOT NULL)""")


@dataclass(frozen=True)
class Document:
    name: str
    database: Path = Path("data/workflows.sqlite3")

    def __post_init__(self):
        if self.name not in NAMES:
            raise ValueError("Unknown feature document")

    def exists(self):
        if not self.database.is_file():
            return False
        with _connection(self.database) as connection:
            return connection.execute("SELECT 1 FROM feature_documents WHERE name=?",
                                      (self.name,)).fetchone() is not None

    def is_file(self):
        return self.exists()

    def read_text(self, **_kwargs):
        return encode(read_document(self))

    def read_bytes(self):
        return encode(read_document(self)).encode("utf-8")


@contextmanager
def _connection(path):
    if not path.is_file():
        raise FileNotFoundError(path)
    with sqlite_connection(path, timeout_seconds=30, synchronous="FULL") as connection:
        yield connection


def encode(payload):
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _evidence(path, name, payload):
    # Preserve exact old rendering input, including fields absent from API facts.
    # A workflow holds only an immutable family evidence reference.
    encoded = encode(payload)
    digest = sha256(encoded.encode("utf-8")).hexdigest()
    with _connection(path) as connection, sqlite_transaction(connection, immediate=True):
        connection.execute("INSERT OR IGNORE INTO saved_board_evidence VALUES (?,?,?)",
                           (digest, name, encoded))
    return {"$family_board_evidence": digest}


def _split(document, payload):
    if not isinstance(payload, dict):
        return payload
    payload = dict(payload)
    for field in EVIDENCE_FIELDS.get(document.name, ()):
        if field in payload:
            payload[field] = _evidence(document.database.parent / "family.sqlite3",
                                       document.name + "/" + field, payload[field])
    return payload


def read_document(document):
    if not isinstance(document, Document):
        return read_json(document)
    if not document.database.is_file():
        raise FileNotFoundError(document.database)
    with _connection(document.database) as connection:
        row = connection.execute("SELECT payload_json FROM feature_documents WHERE name=?",
                                 (document.name,)).fetchone()
    if row is None:
        raise FileNotFoundError(document.name)
    payload = json.loads(row[0])
    for field in EVIDENCE_FIELDS.get(document.name, ()) if isinstance(payload, dict) else ():
        ref = payload.get(field)
        if isinstance(ref, dict) and set(ref) == {"$family_board_evidence"}:
            with _connection(document.database.parent / "family.sqlite3") as connection:
                saved = connection.execute("SELECT payload_json FROM saved_board_evidence WHERE digest=?",
                                           (ref["$family_board_evidence"],)).fetchone()
            if saved is None or sha256(saved[0].encode("utf-8")).hexdigest() != ref["$family_board_evidence"]:
                raise ValueError("Missing or damaged family board evidence")
            payload[field] = json.loads(saved[0])
    return payload


def write_documents(entries):
    """Save one owner's related documents in one transaction."""
    entries = tuple(entries)
    if not entries:
        return
    database = entries[0][0].database
    if any(document.database != database for document, _ in entries):
        raise ValueError("Documents must share one database")
    with _WRITERS_LOCK:
        locks = [_WRITERS.setdefault((database.resolve(), name), RLock())
                 for name in sorted({document.name for document, _ in entries})]
    with ExitStack() as stack:
        for lock in locks:
            stack.enter_context(lock)
        prepared = [(document.name, encode(_split(document, payload))) for document, payload in entries]
        with _connection(database) as connection, sqlite_transaction(connection, immediate=True):
            connection.executemany("INSERT INTO feature_documents VALUES (?,?) ON CONFLICT(name) "
                                   "DO UPDATE SET payload_json=excluded.payload_json", prepared)


def write_document(document, payload, **kwargs):
    if not isinstance(document, Document):
        return write_json_atomic(document, payload, **kwargs)
    write_documents(((document, payload),))


async def write_document_async(document, payload, **kwargs):
    await asyncio.to_thread(write_document, document, payload, **kwargs)


def document_exists(document):
    return document.exists() if isinstance(document, Document) else Path(document).exists()


def save_bonus_revision(current, previous, audit, before, after, entry, expected_revision, audit_limit):
    """Keep a rules edit and all its recovery evidence indivisible."""
    with _connection(current.database) as connection, sqlite_transaction(connection, immediate=True):
        row = connection.execute("SELECT payload_json FROM feature_documents WHERE name=?",
                                 (current.name,)).fetchone()
        saved = json.loads(row[0]) if row else None
        if saved != before or int(saved.get("revision") or 0) != int(expected_revision):
            # Importing a feature exception here would invert the storage boundary.
            raise DocumentConflict("Bonus rule revision changed")
        row = connection.execute("SELECT payload_json FROM feature_documents WHERE name=?",
                                 (audit.name,)).fetchone()
        history = json.loads(row[0]) if row else {"entries": []}
        history["entries"] = [entry, *history["entries"]][:audit_limit]
        connection.executemany("INSERT INTO feature_documents VALUES (?,?) ON CONFLICT(name) "
            "DO UPDATE SET payload_json=excluded.payload_json",
            ((doc.name, encode(payload)) for doc, payload in
             ((current, after), (previous, before), (audit, history))))


class DocumentConflict(RuntimeError):
    """A document changed after the editor loaded it."""
