from pathlib import Path
import unittest
from unittest.mock import patch

from elbow_helper.data.store import FamilyStore
from tests.data.store_fixtures import empty_store
from tests.temp_directory import TemporaryDirectory


class EmptyStoreFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_copies_are_isolated_and_reopen_as_supported_records(self):
        directory = self.enterContext(TemporaryDirectory())
        first = await empty_store(Path(directory) / "first.sqlite3")
        second = await empty_store(Path(directory) / "second.sqlite3")
        await first.begin_run("synthetic", "family", [], 100)
        self.assertEqual(await second.call(lambda c: c.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]), 0)
        reopened = FamilyStore(first.path)
        await reopened.initialize()
        self.assertEqual(await reopened.call(lambda c: c.execute("SELECT status FROM collection_runs").fetchone()[0]), "interrupted")

    async def test_copy_failure_propagates_without_returning_a_ready_store(self):
        directory = self.enterContext(TemporaryDirectory())
        with patch("tests.data.store_fixtures.shutil.copyfile", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                await empty_store(Path(directory) / "failed.sqlite3")
