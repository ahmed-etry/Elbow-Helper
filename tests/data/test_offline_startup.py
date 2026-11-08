"""Offline startup and upgrades retain the record after retired copies are removed."""

import asyncio
from contextlib import chdir, closing
from pathlib import Path
import sqlite3
import subprocess
import sys
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from discord.ext import tasks

from elbow_helper.app import create_bot
from elbow_helper.configuration.files import ACCOUNT_LINKS_DATABASE
from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.data.links.service import LinkService
from elbow_helper.data.reads import PeopleReads
from elbow_helper.data.store import FamilyStore
from elbow_helper.infrastructure.exports import WorkbookWriter

from .record_fixtures import row


class FreshStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_extensions_start_without_old_files_or_network_calls(self):
        # Discord unloads extension modules on close; isolate that from other tests' imports.
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", "import asyncio; from tests.data.test_offline_startup import FreshStartupTests; "
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
                    self.assertFalse((paths.data_root / "clan_health/clan_health.db").exists())
                    self.assertFalse((paths.data_root / "ranked_stats/ranked.sqlite3").exists())
                    for name in (
                        "snapshot_intel/snapshot_intel.json", "leadership_news/state.json",
                        "clan_transfers/transfer_queue.json", "support/created_tickets.json",
                        "hibernation/hibernation.json", "recruitment/trial_data.json",
                        "recruitment/trial_reminders.json", "recruitment/ticket_last_activity.json",
                        "recruitment/applicant_messages.json", "examination/examination_state.json",
                    ):
                        self.assertFalse((paths.data_root / name).exists(), name)
                    with closing(sqlite3.connect(paths.data_root / ACCOUNT_LINKS_DATABASE)) as c:
                        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    self.assertTrue({"suggestions", "ignored_tags", "accounts", "account_links"} <= tables)
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
