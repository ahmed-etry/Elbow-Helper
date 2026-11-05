"""Account ownership changes remain atomic on a fresh family record."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.data.link_fixtures import attach_links
from elbow_helper.features.account_links.database import AccountLinksDbMixin


class AccountLinkTransactionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "links.db"
        self.patcher = patch("elbow_helper.features.account_links.database.DB_PATH", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.links = AccountLinksDbMixin()

    def test_fresh_database_supports_link_reassignment_removal_and_restore(self):
        self.links._init_db()
        self.links._init_db()
        asyncio.run(attach_links(self.links))
        asyncio.run(self.links.upsert_links([
            {"player_tag": "#P0", "discord_user_id": 7, "player_name_last_seen": "Alpha"},
            {"player_tag": "#P2", "discord_user_id": 7, "player_name_last_seen": "Beta"},
        ]))
        before = self.links.get_links_by_tags(["#P0", "#P2", "#P8"])
        asyncio.run(self.links.upsert_link(player_tag="#P0", discord_user_id=8))
        asyncio.run(self.links.delete_link("#P2"))
        asyncio.run(self.links.upsert_link(player_tag="#P8", discord_user_id=8))
        asyncio.run(self.links.restore_links(before))
        self.assertEqual(self.links.get_links_by_tags(before), before)
        self.assertTrue(all("is_primary" not in row for row in self.links.get_all_links().values()))
        with self.links._db_connect() as conn:
            self.assertNotIn("is_primary", [row["name"] for row in conn.execute("PRAGMA table_info(links)")])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
