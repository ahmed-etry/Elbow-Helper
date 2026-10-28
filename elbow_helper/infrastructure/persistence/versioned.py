"""Connection gating and schema checks for adopting known SQLite formats."""

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
import logging
from pathlib import Path
import sqlite3
from threading import Lock

from .migrations import (
    SQLiteMigration, SQLiteMigrationError, UnversionedSQLiteSchemaError,
    run_sqlite_migrations,
)


LOGGER = logging.getLogger(__name__)


class VersionedSQLiteDatabase:
    """Run a feature's upgrades once per file before opening ordinary connections.

    Callers run this synchronous boundary on a worker thread. A rejected upgrade
    is never cached, and journal settings are changed only after it succeeds.
    Post-migration work runs after commit; failures are retried by this instance
    before ordinary connections can open.
    """

    def __init__(
        self, name: str, create: Callable, adopt: Callable, *,
        migrations: Sequence[SQLiteMigration], target_version: int,
        validate: Callable | None = None,
        after_migrate: Callable[[sqlite3.Connection], None] | None = None,
    ):
        self.name = name
        self.create = create
        self.adopt = adopt
        self.migrations = tuple(migrations)
        self.target_version = target_version
        self.validate = validate
        self.after_migrate = after_migrate
        self._pending_after_migrate: set[Path] = set()
        self._ready: set[Path] = set()
        self._lock = Lock()

    def initialize(self, path: str | Path) -> None:
        path = Path(path).absolute()
        with self._lock:
            if path in self._ready:
                return
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(path, timeout=30)) as connection:
                    applied = run_sqlite_migrations(
                        connection,
                        self.migrations,
                        target_version=self.target_version,
                        adopt_unversioned=self.adopt,
                    )
                    if self.validate is None:
                        validate_sqlite_shape(connection, self.create)
                    else:
                        self.validate(connection)
                    if applied and self.after_migrate is not None:
                        self._pending_after_migrate.add(path)
                    if path in self._pending_after_migrate:
                        self.after_migrate(connection)
                        self._pending_after_migrate.remove(path)
                    connection.execute("PRAGMA journal_mode=WAL")
            except (SQLiteMigrationError, sqlite3.Error, OSError):
                LOGGER.exception("Refusing to open %s database at %s", self.name, path)
                raise
            self._ready.add(path)

    def connect(self, path: str | Path, **kwargs) -> sqlite3.Connection:
        path = Path(path).absolute()
        self.initialize(path)
        # Only initialize may create a file. If it disappears after startup,
        # an ordinary connection must fail rather than recreate an empty store.
        return sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, **kwargs)


def validate_sqlite_shape(
    connection: sqlite3.Connection,
    create: Callable[[sqlite3.Connection], None],
    *,
    optional_tables: frozenset[str] = frozenset(),
    missing_columns: Mapping[str, set[str]] | None = None,
    positional_tables: frozenset[str] = frozenset(),
    required_defaults: Mapping[str, Mapping[str, tuple[object, ...]]] | None = None,
    required_nullable: Mapping[str, set[str]] | None = None,
    generated_ids: Mapping[str, str] | None = None,
    omitted_columns: Mapping[str, set[str]] | None = None,
    optional_keys: frozenset[str] = frozenset(),
    warn: bool = True,
) -> None:
    """Require the schema details used by the feature's actual queries.

    Names and identity keys are required; equivalent UNIQUE keys may replace a
    primary key. Other differences only warn, unless the feature identifies a
    positional insert, an omitted default, an explicit NULL or a generated ID
    that depends on the detail. Validation never writes to the supplied file.
    """
    differences = []
    with closing(sqlite3.connect(":memory:")) as reference:
        create(reference)
        tables = [row[0] for row in reference.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        actual_tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for table in tables:
            if table not in actual_tables:
                if table in optional_tables:
                    continue
                raise UnversionedSQLiteSchemaError(f"Missing required table {table}.")
            quoted = _quote(table)
            expected = {row[1]: tuple(row) for row in reference.execute(f"PRAGMA table_info({quoted})")}
            actual = {row[1]: tuple(row) for row in connection.execute(f"PRAGMA table_info({quoted})")}
            missing = expected.keys() - actual.keys() - (missing_columns or {}).get(table, set())
            if missing:
                raise UnversionedSQLiteSchemaError(f"Table {table} is missing columns: {sorted(missing)}.")
            if table in positional_tables and tuple(actual) != tuple(expected):
                raise UnversionedSQLiteSchemaError(f"Table {table} has incompatible column order for positional INSERT.")
            if tuple(name for name in actual if name in expected) != tuple(name for name in expected if name in actual):
                differences.append(f"{table}: column order differs; queries name columns")
            if table not in optional_keys and not _unique_keys(reference, quoted) <= _unique_keys(connection, quoted):
                raise UnversionedSQLiteSchemaError(f"Missing required primary/unique key in {table}.")
            generated_id = (generated_ids or {}).get(table)
            if generated_id and not _is_rowid_alias(connection, quoted, actual, generated_id):
                raise UnversionedSQLiteSchemaError(
                    f"{table}.{generated_id} must be INTEGER PRIMARY KEY: INSERT omits it and needs a generated ID."
                )
            if generated_id and _default_value(actual[generated_id]) is not None:
                raise UnversionedSQLiteSchemaError(
                    f"{table}.{generated_id} must default to NULL so INSERT generates its ID."
                )
            for name, allowed in (required_defaults or {}).get(table, {}).items():
                if name in actual and _default_value(actual[name]) not in allowed:
                    raise UnversionedSQLiteSchemaError(
                        f"Incompatible default: {table}.{name}; INSERT omits this value."
                    )
            for name in (required_nullable or {}).get(table, set()):
                if name in actual and actual[name][3]:
                    raise UnversionedSQLiteSchemaError(
                        f"{table}.{name} must accept NULL: the feature writes missing values explicitly."
                    )
            omitted = (omitted_columns or {}).get(table, set()) | (actual.keys() - expected.keys())
            for name in omitted:
                if name in actual and actual[name][3] and _default_value(actual[name]) is None:
                    if not _is_rowid_alias(connection, quoted, actual, name):
                        raise UnversionedSQLiteSchemaError(
                            f"{table}.{name} is NOT NULL without a usable default; INSERT omits this column."
                        )
            for name in actual.keys() - expected.keys():
                differences.append(f"{table}.{name}: extra column")
            for name in expected.keys() & actual.keys():
                wanted, found = expected[name], actual[name]
                for label, index in (("declared type", 2), ("nullability", 3), ("default", 4)):
                    if wanted[index] != found[index]:
                        differences.append(f"{table}.{name}: {label} differs")
                if wanted[5] != found[5]:
                    differences.append(f"{table}.{name}: primary key declaration differs")
            required_foreign_keys = _foreign_keys(reference, quoted)
            for key in sorted(required_foreign_keys - _foreign_keys(connection, quoted)):
                differences.append(f"{table}: missing foreign key {key}")
    if warn:
        for difference in differences:
            LOGGER.warning("SQLite schema differs from fresh schema: %s", difference)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _default_value(column: tuple) -> object:
    if column[4] is None:
        return None
    # Apply the column's affinity too: INTEGER DEFAULT '0' also inserts zero.
    with closing(sqlite3.connect(":memory:")) as probe:
        try:
            probe.execute(f"CREATE TABLE value_probe (value {_quote(column[2])} DEFAULT {column[4]})")
            probe.execute("INSERT INTO value_probe DEFAULT VALUES")
            return probe.execute("SELECT value FROM value_probe").fetchone()[0]
        except sqlite3.Error as error:
            raise UnversionedSQLiteSchemaError(f"Cannot establish default for {column[1]}.") from error


def _is_rowid_alias(connection, table, columns, name) -> bool:
    column = columns[name]
    return (
        column[2].upper() == "INTEGER" and column[5] == 1
        and sum(bool(row[5]) for row in columns.values()) == 1
        and not any(row[3] == "pk" for row in connection.execute(f"PRAGMA index_list({table})"))
    )


def _unique_keys(connection: sqlite3.Connection, table: str) -> set[frozenset[str]]:
    keys = set()
    columns = {row[1]: tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")}
    for name in columns:
        if _is_rowid_alias(connection, table, columns, name):
            keys.add(frozenset({name}))
    for index in connection.execute(f"PRAGMA index_list({table})"):
        if index[2] and not index[4]:
            names = [row[2] for row in connection.execute(f"PRAGMA index_info({_quote(index[1])})")]
            if all(name is not None for name in names):
                keys.add(frozenset(names))
    return keys


def _foreign_keys(connection: sqlite3.Connection, table: str) -> set[tuple]:
    # SQLite may renumber constraints; their declared IDs are not meaningful.
    return {tuple(row[1:]) for row in connection.execute(f"PRAGMA foreign_key_list({table})")}
