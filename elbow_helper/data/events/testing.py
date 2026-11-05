"""Memory-backed store double with the same transactions and failure semantics."""

from contextlib import contextmanager
import sqlite3
from threading import Lock

from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction
from . import EventsStore
from .schema import create_schema


class MemoryEventsStore(EventsStore):
    def __init__(self):
        self._writer = Lock()
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        create_schema(self.connection)
        self.fail_next_write = False
        self._deletion_handlers = {}

    @contextmanager
    def _connection(self):
        yield self.connection

    def _call(self, operation, args, kwargs):
        with self._writer, sqlite_transaction(self.connection, immediate=True):
            result = operation(self.connection, *args, **kwargs)
            if self.fail_next_write:
                self.fail_next_write = False
                raise sqlite3.OperationalError("Synthetic workflow write failure")
            return result

    def close(self):
        self.connection.close()
