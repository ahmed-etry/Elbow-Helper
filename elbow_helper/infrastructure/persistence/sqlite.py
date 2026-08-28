"""Reusable SQLite connection and transaction mechanics."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Literal


SQLiteSynchronousMode = Literal["OFF", "NORMAL", "FULL", "EXTRA"]
SQLiteRowFactory = Callable[
    [sqlite3.Cursor, tuple[object, ...]],
    object,
]
_SYNCHRONOUS_MODES = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})


@contextmanager
def sqlite_connection(
    path: str | Path,
    *,
    timeout_seconds: float = 5.0,
    busy_timeout_ms: int | None = None,
    foreign_keys: bool = True,
    synchronous: SQLiteSynchronousMode = "NORMAL",
    row_factory: SQLiteRowFactory | None = sqlite3.Row,
) -> Iterator[sqlite3.Connection]:
    """Open a consistently configured SQLite connection and always close it."""
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds cannot be negative")
    if busy_timeout_ms is not None and busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms cannot be negative")

    synchronous_mode = synchronous.upper()
    if synchronous_mode not in _SYNCHRONOUS_MODES:
        raise ValueError(f"Unsupported SQLite synchronous mode: {synchronous}")

    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=timeout_seconds)
    connection.row_factory = row_factory
    try:
        connection.execute(
            f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}"
        )
        connection.execute(f"PRAGMA synchronous={synchronous_mode}")
        if busy_timeout_ms is not None:
            connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        yield connection
    finally:
        connection.close()


@contextmanager
def sqlite_transaction(
    connection: sqlite3.Connection,
    *,
    immediate: bool = False,
) -> Iterator[None]:
    """Commit a unit of work, or roll all of it back when an error escapes."""
    if connection.in_transaction:
        raise RuntimeError("Cannot start a nested SQLite transaction.")

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
