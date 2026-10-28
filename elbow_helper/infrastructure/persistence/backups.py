"""Verified, atomic online copies of the application's SQLite databases.

All functions in this module perform blocking work and belong in a worker thread.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import monotonic

from elbow_helper.configuration.files import SQLITE_DATABASES


LOGGER = logging.getLogger(__name__)
BACKUP_MAX_AGE = timedelta(hours=24)
BACKUP_PAGES_PER_STEP = 256
BACKUP_TIMEOUT_SECONDS = 300


@dataclass(frozen=True, slots=True)
class BackupResult:
    copied: int
    failed: int
    total_bytes: int


def backup_database(source: Path, destination: Path) -> int:
    """Publish a standalone, checked copy, preserving the previous one on error."""
    if not source.is_file():
        raise FileNotFoundError(f"Database source is missing: {source}")

    deadline = monotonic() + BACKUP_TIMEOUT_SECONDS

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if monotonic() >= deadline:
            raise TimeoutError("Database backup exceeded its time limit")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f".{destination.name}-", dir=destination.parent) as directory:
        temporary = Path(directory) / "copy.sqlite3"
        with (
            closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as reader,
            closing(sqlite3.connect(temporary)) as writer,
        ):
            reader.backup(writer, pages=BACKUP_PAGES_PER_STEP, progress=progress, sleep=0.05)
            # A WAL source must still produce one independently restorable file.
            writer.execute("PRAGMA journal_mode=DELETE")
            checked = writer.execute("PRAGMA quick_check").fetchall()
            if checked != [("ok",)]:
                raise sqlite3.DatabaseError(f"Backup quick_check failed: {checked}")
        total_bytes = temporary.stat().st_size
        temporary.replace(destination)
    return total_bytes


def backup_databases(data_root: Path) -> BackupResult:
    """Copy each known database independently and log one run summary."""
    copied = failed = total_bytes = 0
    for relative_path in SQLITE_DATABASES:
        try:
            size = backup_database(
                data_root / relative_path,
                data_root / "backups" / relative_path,
            )
        except Exception as error:
            # A failed file must not prevent the remaining databases being copied.
            failed += 1
            LOGGER.warning("Database backup failed for %s: %s", relative_path, error)
        else:
            copied += 1
            total_bytes += size
    LOGGER.info("Database backups: copied=%s failed=%s bytes=%s", copied, failed, total_bytes)
    return BackupResult(copied, failed, total_bytes)


def backups_need_catch_up(data_root: Path, now: datetime) -> bool:
    """Catch up if any expected copy is missing, unreadable, or over a day old."""
    for relative_path in SQLITE_DATABASES:
        try:
            modified = (data_root / "backups" / relative_path).stat().st_mtime
        except FileNotFoundError:
            return True
        except OSError as error:
            LOGGER.warning("Cannot check database backup age for %s: %s", relative_path, error)
            return True
        if now.timestamp() - modified > BACKUP_MAX_AGE.total_seconds():
            return True
    return False
