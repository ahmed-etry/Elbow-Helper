"""Offline startup and upgrades retain the record after retired copies are removed."""

import asyncio
from contextlib import chdir, closing
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from discord.ext import tasks

from elbow_helper.app import create_bot
from elbow_helper.configuration.files import (
    ACCOUNT_LINKS_DATABASE, CLAN_HEALTH_DATABASE, RANKED_DATABASE, SQLITE_DATABASES,
)
from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.data.links import LinkService
from elbow_helper.data.reads import PeopleReads
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.schema import MIGRATIONS, SCHEMA_VERSION
from elbow_helper.infrastructure.exports import WorkbookWriter
from elbow_helper.infrastructure.persistence.migrations import (
    SQLiteMigration, SQLiteMigrationError, UnversionedSQLiteSchemaError,
)

from .record_fixtures import PeopleFixture, row


class LiveRecordUpgradeTests(PeopleFixture):
    async def test_live_version_16_upgrades_without_changing_facts_or_copy_receipts(self):
        # Main's deployed record is version 16; later upgrades must retain its facts.
        await self.execute("DROP TABLE member_last_messages")
        await self.execute("PRAGMA user_version=16")
        before = await self.dump()
        restart = FamilyStore(self.store.path)
        await restart.initialize()
        after = await restart.call(lambda c: tuple(c.iterdump()))
        self.assertEqual(set(before) - set(after), set())
        self.assertEqual(await restart.call(lambda c: c.execute("PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
        self.assertEqual(await restart.call(lambda c: c.execute("PRAGMA foreign_key_check").fetchall()), [])
        links = LinkService(restart)
        self.addAsyncCleanup(links.close)
        self.legacy.unlink()
        self.assertEqual((await links.read_links())["#P0"]["discord_user_id"], 100)
        receipts = await self.rows("SELECT * FROM imports")
        import_history = await self.rows("SELECT * FROM link_imports")
        await links.upsert_link(**row(owner=300), at=300)
        self.assertEqual(await self.rows("SELECT * FROM imports"), receipts)
        self.assertEqual(await self.rows("SELECT * FROM link_imports"), import_history)
        self.assertFalse(self.legacy.exists())

    async def test_failed_live_version_upgrade_leaves_facts_and_version_unchanged(self):
        await self.execute("DROP TABLE member_last_messages")
        await self.execute("PRAGMA user_version=16")
        before = await self.dump()

        def fail(c):
            MIGRATIONS[16].apply(c)
            raise OSError("synthetic interrupted upgrade")

        failing = (*MIGRATIONS[:16], SQLiteMigration(17, "failure", fail), *MIGRATIONS[17:])
        with patch("elbow_helper.data.store.MIGRATIONS", failing):
            with self.assertRaises(SQLiteMigrationError):
                await FamilyStore(self.store.path).initialize()
        self.assertEqual(await self.dump(), before)
        self.assertEqual((await self.rows("PRAGMA user_version"))[0]["user_version"], 16)
        await FamilyStore(self.store.path).initialize()


class FreshStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_extensions_start_without_old_files_or_network_calls(self):
        # Discord unloads extension modules on close; isolate that from other tests' imports.
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", "import asyncio; from tests.data.test_old_code_removal import FreshStartupTests; "
             "asyncio.run(FreshStartupTests()._assert_offline_startup())"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    async def _assert_offline_startup(self):
        with TemporaryDirectory(dir=Path.cwd()) as directory, chdir(directory):
            paths = ApplicationPaths.from_project_root(Path(directory))
            clash = SimpleNamespace(configured=False, get=AsyncMock(side_effect=AssertionError("Clash called")))
            model = SimpleNamespace(configured=False, start_session=AsyncMock(side_effect=AssertionError("LLM called")))
            bot = create_bot(paths, clash, model, SimpleNamespace(configured=False), WorkbookWriter())
            bot.application_info = AsyncMock(return_value=SimpleNamespace(owner=None, team=None))
            self.assertFalse(paths.data_root.exists())
            (paths.data_root / "agent" / "knowledge").mkdir(parents=True)
            # Exercise every real extension and schema initializer. Background loops and Discord sync stay offline.
            with patch.object(tasks.Loop, "start"), patch(
                "elbow_helper.core.lifecycle.sync_guild_commands", AsyncMock(return_value=()),
            ):
                async with bot:
                    await bot.setup_hook()
                    self.assertEqual(len(bot.extension_report.loaded), len(bot.required_extensions))
                    self.assertFalse(bot.extension_report.required_failures)
                    self.assertEqual(await bot.link_service.read_links(), {})
                    await bot.link_service.upsert_link(**row(), at=300)
                    self.assertEqual((await bot.people_reads.current_links(400))["rows"][0]["discord_user_id"], 100)
                    self.assertFalse((paths.data_root / CLAN_HEALTH_DATABASE).exists())
                    self.assertFalse((paths.data_root / RANKED_DATABASE).exists())
                    with closing(sqlite3.connect(paths.data_root / ACCOUNT_LINKS_DATABASE)) as c:
                        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    self.assertEqual(tables, {"suggestions", "ignored_tags"})
            clash.get.assert_not_awaited()
            model.start_session.assert_not_awaited()

    async def test_fresh_links_need_no_import_marker_and_keep_failures_atomic(self):
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            store = FamilyStore(Path(directory) / "family.sqlite3")
            service = LinkService(store)
            self.addAsyncCleanup(service.close)
            self.assertEqual(await service.read_links(), {})
            self.assertTrue((await PeopleReads(store).current_links(100))["coverage"]["complete"])
            await service.upsert_link(**row(), at=200)
            self.assertTrue((await PeopleReads(store).owners(["#P0"], 10**12))["coverage"]["complete"])
            before = await store.call(lambda c: tuple(c.iterdump()))

            def refuse(c):
                c.execute("CREATE TRIGGER refuse_write BEFORE INSERT ON link_operations "
                          "BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END")
                c.commit()
            await store.call(refuse)
            with self.assertRaises(sqlite3.IntegrityError):
                await service.upsert_link(**row(owner=300), at=300)
            self.assertEqual((await service.read_links())["#P0"]["discord_user_id"], 100)
            after = await store.call(lambda c: tuple(c.iterdump()))
            self.assertFalse(set(before) - set(after))
            self.assertEqual(await store.call(lambda c: c.execute("SELECT COUNT(*) FROM imports").fetchone()[0]), 0)

    def test_retired_databases_remain_in_the_backup_inventory(self):
        self.assertTrue({CLAN_HEALTH_DATABASE, RANKED_DATABASE, ACCOUNT_LINKS_DATABASE} <= set(SQLITE_DATABASES))


class ReviewFileTests(unittest.TestCase):
    def test_old_unversioned_links_backup_is_refused_unchanged(self):
        from elbow_helper.features.account_links.schema import DATABASE
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "old-links.sqlite3"
            with closing(sqlite3.connect(path)) as c, c:
                c.execute("CREATE TABLE links (player_tag TEXT PRIMARY KEY, is_primary INTEGER)")
                c.execute("INSERT INTO links VALUES ('#P0',1)")
            before = path.read_bytes()
            with self.assertLogs("elbow_helper.infrastructure.persistence.versioned", level="ERROR"):
                with self.assertRaises(UnversionedSQLiteSchemaError):
                    DATABASE.initialize(path)
            self.assertEqual(path.read_bytes(), before)

    def test_current_review_file_keeps_old_link_rows_without_reading_or_upgrading_them(self):
        from elbow_helper.features.account_links.schema import DATABASE
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "links.sqlite3"
            DATABASE.initialize(path)
            with closing(sqlite3.connect(path)) as c, c:
                c.execute("CREATE TABLE links (old_data BLOB)")
                c.execute("INSERT INTO links VALUES (x'00ff')")
                c.execute("INSERT INTO ignored_tags VALUES ('#P0')")
                before = tuple(c.iterdump())
            DATABASE._ready.discard(path.absolute())
            with closing(DATABASE.connect(path)) as c:
                self.assertEqual(tuple(c.iterdump()), before)
                self.assertEqual(c.execute("SELECT * FROM ignored_tags").fetchall(), [("#P0",)])
