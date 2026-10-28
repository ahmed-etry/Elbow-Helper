from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper import app
from elbow_helper.configuration.files import SQLITE_DATABASES
from elbow_helper.core.backups import BACKUP_TIME_UTC, DatabaseBackupJob
from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.infrastructure.persistence import backups


def create_database(path: Path, value: str = "saved") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE state(value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES (?)", (value,))
        connection.execute("PRAGMA user_version=7")


def contents(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return list(connection.iterdump())


class DatabaseCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source # 50%.sqlite3"
        self.destination = self.root / "backups" / "source.sqlite3"
        create_database(self.source)

    def assert_no_temporary_files(self) -> None:
        self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])

    def test_copy_matches_source_and_overwrites_only_after_check(self) -> None:
        original = self.source.read_bytes()
        size = backups.backup_database(self.source, self.destination)
        self.assertEqual(contents(self.destination), contents(self.source))
        self.assertEqual(size, self.destination.stat().st_size)
        self.assertEqual(self.source.read_bytes(), original)
        with closing(sqlite3.connect(self.destination)) as copy:
            self.assertEqual(copy.execute("PRAGMA user_version").fetchone(), (7,))
        with closing(sqlite3.connect(self.source)) as source, source:
            source.execute("INSERT INTO state VALUES ('new')")
        backups.backup_database(self.source, self.destination)
        self.assertEqual(contents(self.destination), contents(self.source))
        self.assert_no_temporary_files()

    def test_source_is_opened_read_only_using_an_encoded_uri(self) -> None:
        connect = sqlite3.connect
        test = self
        inspected = []

        class ReadOnlyConnection(sqlite3.Connection):
            def backup(self, target, **kwargs):
                with test.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                    self.execute("INSERT INTO state VALUES ('forbidden')")
                inspected.append(kwargs["pages"])
                return super().backup(target, **kwargs)

        def open_connection(path, **kwargs):
            if kwargs.get("uri"):
                self.assertIn("%23", path)
                self.assertIn("%25", path)
                self.assertTrue(path.endswith("?mode=ro"))
                kwargs["factory"] = ReadOnlyConnection
            return connect(path, **kwargs)

        with patch.object(backups.sqlite3, "connect", side_effect=open_connection):
            backups.backup_database(self.source, self.destination)
        self.assertEqual(inspected, [backups.BACKUP_PAGES_PER_STEP])
        self.assertGreater(inspected[0], 0)

    def test_commits_during_incremental_copy_produce_a_valid_standalone_copy(self) -> None:
        connect = sqlite3.connect
        for journal in ("DELETE", "WAL"):
            with self.subTest(journal=journal), closing(connect(self.source)) as writer:
                writer.execute(f"PRAGMA journal_mode={journal}")
                writer.execute("CREATE TABLE IF NOT EXISTS payload(value BLOB)")
                writer.execute("INSERT INTO payload VALUES (zeroblob(131072))")
                writer.commit()
                committed = []

                class ConcurrentConnection(sqlite3.Connection):
                    def backup(self, target, **kwargs):
                        original_progress = kwargs["progress"]

                        def progress(status, remaining, total):
                            if remaining > 0 and not committed:
                                writer.execute("INSERT INTO state VALUES ('during copy')")
                                writer.commit()
                                committed.append(True)
                            original_progress(status, remaining, total)

                        kwargs["progress"] = progress
                        return super().backup(target, **kwargs)

                def open_connection(path, **kwargs):
                    if kwargs.get("uri"):
                        kwargs["factory"] = ConcurrentConnection
                    return connect(path, **kwargs)

                with (
                    patch.object(backups, "BACKUP_PAGES_PER_STEP", 1),
                    patch.object(backups.sqlite3, "connect", side_effect=open_connection),
                ):
                    backups.backup_database(self.source, self.destination)
                self.assertEqual(committed, [True])
                self.assertEqual(contents(self.destination), contents(self.source))
                # The source's WAL is still open here; the copy needs no sidecars.
                self.assert_no_temporary_files()
                with closing(connect(self.destination)) as copy:
                    self.assertEqual(copy.execute("PRAGMA quick_check").fetchall(), [("ok",)])
                    self.assertEqual(copy.execute("PRAGMA journal_mode").fetchone(), ("delete",))

    def test_failed_check_keeps_previous_good_copy(self) -> None:
        backups.backup_database(self.source, self.destination)
        previous = self.destination.read_bytes()
        connect = sqlite3.connect

        class FailedCheckConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == "PRAGMA quick_check":
                    return SimpleNamespace(fetchall=lambda: [("ok",), ("damaged page",)])
                return super().execute(sql, *args)

        def open_connection(path, **kwargs):
            if not kwargs.get("uri"):
                kwargs["factory"] = FailedCheckConnection
            return connect(path, **kwargs)

        with (
            patch.object(backups.sqlite3, "connect", side_effect=open_connection),
            self.assertRaisesRegex(sqlite3.DatabaseError, "quick_check failed"),
        ):
            backups.backup_database(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assert_no_temporary_files()

    def test_corrupt_source_and_failed_rename_keep_previous_copy(self) -> None:
        backups.backup_database(self.source, self.destination)
        previous = self.destination.read_bytes()
        with (
            patch.object(Path, "replace", side_effect=PermissionError("cannot replace")),
            self.assertRaises(PermissionError),
        ):
            backups.backup_database(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assert_no_temporary_files()
        self.source.write_bytes(b"not a SQLite database")
        with self.assertRaises(sqlite3.DatabaseError):
            backups.backup_database(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assert_no_temporary_files()

    def test_timeout_keeps_previous_copy_and_cleans_up(self) -> None:
        backups.backup_database(self.source, self.destination)
        previous = self.destination.read_bytes()
        with (
            patch.object(backups, "monotonic", side_effect=[0, backups.BACKUP_TIMEOUT_SECONDS + 1]),
            self.assertRaises(TimeoutError),
        ):
            backups.backup_database(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assert_no_temporary_files()

    def test_locked_source_times_out_instead_of_stopping_the_run(self) -> None:
        backups.backup_database(self.source, self.destination)
        previous = self.destination.read_bytes()
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            try:
                with patch.object(backups, "BACKUP_TIMEOUT_SECONDS", 0.01), self.assertRaises(TimeoutError):
                    backups.backup_database(self.source, self.destination)
            finally:
                writer.rollback()
        self.assertEqual(self.destination.read_bytes(), previous)
        self.assert_no_temporary_files()


class DatabaseRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_inventory_covers_all_eleven_databases_without_scanning_other_files(self) -> None:
        expected = {
            "clan_links/links.sqlite3", "achievements/achievements.db",
            "agent/agent.sqlite3", "agent/transcripts.sqlite3",
            "agent/research_jobs.sqlite3", "agent/actions.sqlite3",
            "clan_health/clan_health.db", "ranked_stats/ranked.sqlite3",
            "records/records.sqlite3", "rosters/rosters.sqlite3", "family/family.sqlite3",
        }
        self.assertEqual({path.as_posix() for path in SQLITE_DATABASES}, expected)
        self.assertEqual(len(SQLITE_DATABASES), 11)
        for relative in SQLITE_DATABASES:
            create_database(self.root / relative)
        create_database(self.root / "unowned.db")
        with self.assertLogs(backups.LOGGER, level="INFO") as logs:
            result = backups.backup_databases(self.root)
        self.assertEqual((result.copied, result.failed), (11, 0))
        self.assertEqual(len(logs.records), 1)
        total_size = 0
        for relative in SQLITE_DATABASES:
            destination = self.root / "backups" / relative
            self.assertEqual(contents(destination), contents(self.root / relative))
            total_size += destination.stat().st_size
        self.assertEqual(result.total_bytes, total_size)
        self.assertIn(f"copied=11 failed=0 bytes={total_size}", logs.output[0])
        self.assertFalse((self.root / "backups" / "unowned.db").exists())

    def test_missing_and_corrupt_sources_warn_and_do_not_block_other_copies(self) -> None:
        paths = (Path("missing.db"), Path("broken.db"), Path("valid.db"))
        (self.root / paths[1]).write_bytes(b"broken")
        create_database(self.root / paths[2])
        with (
            patch.object(backups, "SQLITE_DATABASES", paths),
            self.assertLogs(backups.LOGGER, level="INFO") as logs,
        ):
            result = backups.backup_databases(self.root)
        self.assertEqual((result.copied, result.failed), (1, 2))
        self.assertEqual([record.levelname for record in logs.records], ["WARNING", "WARNING", "INFO"])
        self.assertIn("missing.db", logs.output[0])
        self.assertIn("broken.db", logs.output[1])
        self.assertFalse((self.root / paths[0]).exists())
        self.assertEqual(contents(self.root / "backups" / paths[2]), contents(self.root / paths[2]))

    def test_unexpected_file_failure_is_isolated_and_logged(self) -> None:
        with (
            patch.object(backups, "SQLITE_DATABASES", (Path("one.db"), Path("two.db"))),
            patch.object(backups, "backup_database", side_effect=[RuntimeError("failed"), 25]),
            self.assertLogs(backups.LOGGER, level="INFO") as logs,
        ):
            result = backups.backup_databases(self.root)
        self.assertEqual(result, backups.BackupResult(1, 1, 25))
        self.assertEqual(len(logs.records), 2)

    def test_catch_up_uses_each_copy_age_including_missing_and_unreadable_files(self) -> None:
        paths = (Path("one.db"), Path("two.db"))
        now = datetime(2026, 10, 28, 6, tzinfo=timezone.utc)
        with patch.object(backups, "SQLITE_DATABASES", paths):
            self.assertTrue(backups.backups_need_catch_up(self.root, now))
            for relative in paths:
                destination = self.root / "backups" / relative
                create_database(destination)
                modified = (now - timedelta(hours=24)).timestamp()
                os.utime(destination, (modified, modified))
            self.assertFalse(backups.backups_need_catch_up(self.root, now))
            self.assertTrue(backups.backups_need_catch_up(self.root, now + timedelta(seconds=1)))
            recent = now.timestamp()
            os.utime(self.root / "backups" / paths[1], (recent, recent))
            self.assertTrue(backups.backups_need_catch_up(self.root, now + timedelta(seconds=1)))
            with (
                patch.object(Path, "stat", side_effect=PermissionError("cannot read age")),
                self.assertLogs(backups.LOGGER, level="WARNING"),
            ):
                self.assertTrue(backups.backups_need_catch_up(self.root, now))


class BackupSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_schedule_uses_0400_utc_across_day_and_timezone_boundaries(self) -> None:
        job = DatabaseBackupJob(Path("unused"), ready=asyncio.Event())
        self.assertEqual(job.daily.time, [BACKUP_TIME_UTC])
        cases = (
            (datetime(2026, 10, 28, 3, 59, tzinfo=timezone.utc), datetime(2026, 10, 28, 4, tzinfo=timezone.utc)),
            (datetime(2026, 10, 28, 4, tzinfo=timezone.utc), datetime(2026, 10, 28, 4, tzinfo=timezone.utc)),
            (datetime(2026, 12, 31, 4, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, 4, tzinfo=timezone.utc)),
            (datetime(2026, 10, 28, 5, 1, tzinfo=timezone(timedelta(hours=1))), datetime(2026, 10, 29, 4, tzinfo=timezone.utc)),
        )
        for now, expected in cases:
            with self.subTest(now=now):
                # Exercise the scheduler used by this configured loop.
                self.assertEqual(job.daily._get_next_sleep_time(now), expected)

    async def test_catch_up_waits_for_startup_and_runs_file_work_off_the_event_loop(self) -> None:
        ready = asyncio.Event()
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        threads = []
        root = Path("unused")

        def due(data_root, now):
            self.assertEqual(data_root, root)
            self.assertEqual(now.tzinfo, timezone.utc)
            threads.append(threading.get_ident())
            return True

        def copy(data_root):
            self.assertEqual(data_root, root)
            threads.append(threading.get_ident())
            loop.call_soon_threadsafe(started.set)
            if not release.wait(2):
                raise TimeoutError("test did not release backup worker")

        with (
            patch("elbow_helper.core.backups.backups_need_catch_up", side_effect=due) as check,
            patch("elbow_helper.core.backups.backup_databases", side_effect=copy) as worker,
        ):
            try:
                async with DatabaseBackupJob(root, ready=ready) as job:
                    await asyncio.sleep(0)
                    check.assert_not_called()
                    ready.set()
                    await asyncio.wait_for(started.wait(), 1)
                    self.assertFalse(job.daily.get_task().done())
                    self.assertEqual(len(threads), 2)
                    self.assertTrue(all(thread != loop_thread for thread in threads))
                    release.set()
            finally:
                release.set()
        worker.assert_called_once_with(root)
        self.assertTrue(job.daily.get_task().done())

    async def test_fresh_copies_skip_startup_copy_but_daily_still_runs(self) -> None:
        ready = asyncio.Event()
        ready.set()
        job = DatabaseBackupJob(Path("unused"), ready=ready)
        loop_thread = threading.get_ident()

        def copy(_root):
            self.assertNotEqual(threading.get_ident(), loop_thread)

        with (
            patch("elbow_helper.core.backups.backups_need_catch_up", return_value=False),
            patch("elbow_helper.core.backups.backup_databases", side_effect=copy) as worker,
        ):
            await job.prepare()
            worker.assert_not_called()
            await job.daily()
        worker.assert_called_once()

    async def test_cancellation_waits_for_worker_cleanup(self) -> None:
        started = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()
        loop = asyncio.get_running_loop()

        def copy(_root):
            loop.call_soon_threadsafe(started.set)
            try:
                release.wait(2)
            finally:
                finished.set()

        job = DatabaseBackupJob(Path("unused"), ready=asyncio.Event())
        with patch("elbow_helper.core.backups.backup_databases", side_effect=copy):
            task = asyncio.create_task(job.run_once())
            try:
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(finished.is_set())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_application_owns_backup_job_and_closes_it_when_bot_fails(self) -> None:
        paths = ApplicationPaths.from_project_root(Path(__file__).resolve().parents[2])
        settings = SimpleNamespace(
            google_oauth_client_id=None, google_oauth_client_secret=None,
            google_oauth_refresh_token=None, google_drive_folder_id=None,
            deepseek_api_key=None, coc_api_key=None,
            require_discord_token=lambda: "test-token",
        )
        bot = MagicMock(boot_complete=asyncio.Event())
        bot.start = AsyncMock(side_effect=RuntimeError("bot stopped"))
        job = MagicMock()
        with (
            patch.object(app.ApplicationPaths, "discover", return_value=paths),
            patch.object(app, "load_runtime_settings", return_value=settings),
            patch.object(app, "configure_logging"),
            patch.object(app, "install_asyncio_exception_handler"),
            patch.object(app, "GoogleSheetsPublisher"),
            patch.object(app, "WorkbookWriter"),
            patch.object(app, "DeepSeekTextClient"),
            patch.object(app, "ClashClient"),
            patch.object(app, "create_bot", return_value=bot),
            patch.object(app, "DatabaseBackupJob", return_value=job) as construct,
            self.assertRaisesRegex(RuntimeError, "bot stopped"),
        ):
            await app.main()
        construct.assert_called_once_with(paths.data_root, ready=bot.boot_complete)
        job.__aenter__.assert_awaited_once()
        job.__aexit__.assert_awaited_once()
        bot.start.assert_awaited_once_with("test-token")
