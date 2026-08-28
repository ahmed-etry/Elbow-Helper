"""Small, transactional SQLite schema migration runner."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
import sqlite3

from .sqlite import sqlite_transaction


class SQLiteMigrationError(RuntimeError):
    """Base error for an unsafe or failed schema migration."""


class UnsupportedSQLiteVersionError(SQLiteMigrationError):
    """Raised when a database version is not supported by the application."""


class UnversionedSQLiteSchemaError(SQLiteMigrationError):
    """Raised when an existing schema has no declared version."""


@dataclass(frozen=True, slots=True)
class SQLiteMigration:
    """One schema transition ending at ``version``."""

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def sqlite_schema_version(connection: sqlite3.Connection) -> int:
    """Return SQLite's application-controlled schema version."""
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def run_sqlite_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[SQLiteMigration],
    *,
    target_version: int,
) -> tuple[int, ...]:
    """Apply pending migrations atomically and return their version numbers."""
    ordered = tuple(migrations)
    _validate_migrations(ordered, target_version=target_version)

    current_version = sqlite_schema_version(connection)
    if current_version > target_version:
        raise UnsupportedSQLiteVersionError(
            f"Database version {current_version} is newer than supported "
            f"version {target_version}."
        )
    if current_version == target_version:
        return ()

    known_versions = {migration.version for migration in ordered}
    if current_version != 0 and current_version not in known_versions:
        raise UnsupportedSQLiteVersionError(
            f"Database version {current_version} has no supported migration path."
        )
    if current_version == 0 and _has_user_schema(connection):
        raise UnversionedSQLiteSchemaError(
            "Refusing to migrate a non-empty database without a schema version."
        )

    pending = tuple(
        migration
        for migration in ordered
        if current_version < migration.version <= target_version
    )
    if not pending or pending[-1].version != target_version:
        raise SQLiteMigrationError(
            f"No migration path reaches target version {target_version}."
        )

    active_migration: SQLiteMigration | None = None
    try:
        with sqlite_transaction(connection, immediate=True):
            for active_migration in pending:
                active_migration.apply(connection)
                if not connection.in_transaction:
                    raise SQLiteMigrationError(
                        f"Migration {active_migration.version} committed its own "
                        "transaction."
                    )
            connection.execute(f"PRAGMA user_version={target_version}")
    except SQLiteMigrationError:
        raise
    except Exception as error:
        version = active_migration.version if active_migration else "unknown"
        name = active_migration.name if active_migration else "unknown"
        raise SQLiteMigrationError(
            f"SQLite migration {version} ({name}) failed."
        ) from error

    return tuple(migration.version for migration in pending)


def _validate_migrations(
    migrations: tuple[SQLiteMigration, ...],
    *,
    target_version: int,
) -> None:
    if (
        isinstance(target_version, bool)
        or not isinstance(target_version, int)
        or target_version < 0
    ):
        raise ValueError("target_version must be a non-negative integer")
    if target_version == 0:
        if migrations:
            raise ValueError("Version-zero databases cannot define migrations.")
        return
    if not migrations:
        raise ValueError("At least one migration is required.")

    previous_version = 0
    for migration in migrations:
        if (
            isinstance(migration.version, bool)
            or not isinstance(migration.version, int)
            or migration.version <= 0
        ):
            raise ValueError("Migration versions must be positive integers.")
        if migration.version <= previous_version:
            raise ValueError(
                "Migrations must have unique, strictly increasing versions."
            )
        if not isinstance(migration.name, str) or not migration.name.strip():
            raise ValueError("Migration names cannot be empty.")
        if not callable(migration.apply):
            raise TypeError("Migration apply handlers must be callable.")
        previous_version = migration.version

    if migrations[-1].version != target_version:
        raise ValueError(
            "The final migration version must equal the target version."
        )


def _has_user_schema(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
          AND type IN ('table', 'index', 'view', 'trigger')
        LIMIT 1
        """
    ).fetchone()
    return row is not None
