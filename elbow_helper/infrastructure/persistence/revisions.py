"""Read-only SQLite commit revisions, including writes from other processes."""

from collections import OrderedDict
from pathlib import Path
import sqlite3
from threading import Lock
from uuid import uuid4


MAX_DATABASES = 8
_connections = OrderedDict()
_lock = Lock()


def release_sqlite_revision(path):
    """Release observers when their database is retired or replaced."""
    resolved = str(Path(path).resolve())
    with _lock:
        for identity in tuple(_connections):
            if identity[0] == resolved:
                _connections.pop(identity)[0].close()


def sqlite_revision(path):
    """data_version needs a stable connection; never use a fresh connection's value.

    This deliberately invalidates the whole source on any commit. It also catches
    imports, backfills, corrections and deletes without collector-specific hooks.
    No transaction is left open, so WAL checkpointing can proceed normally.
    """
    path = Path(path).resolve()
    with _lock:
        try:
            identity = (str(path), path.stat().st_ino)
            if identity not in _connections:
                while len(_connections) >= MAX_DATABASES:
                    _connections.popitem(last=False)[1][0].close()
                connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                             check_same_thread=False, timeout=1)
                _connections[identity] = (connection, uuid4().hex)
            connection, epoch = _connections[identity]
            _connections.move_to_end(identity)
            version = connection.execute('PRAGMA data_version').fetchone()[0]
            return epoch + ':' + str(version)
        except (OSError, sqlite3.Error):
            return None
