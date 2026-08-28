from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from elbow_helper.features.account_links.database import AccountLinksDbMixin
from elbow_helper.features.account_links.cog import AccountLinks


class _LinksHarness(AccountLinksDbMixin):
    pass


class AccountLinksTests(unittest.TestCase):
    def test_account_commands_use_member_and_tag_option_names(self) -> None:
        commands = {command.name: command for command in AccountLinks.account_group.commands}

        self.assertEqual([parameter.name for parameter in commands["add"].parameters], ["member", "tags"])
        self.assertEqual([parameter.name for parameter in commands["remove"].parameters], ["tags"])
        self.assertEqual([parameter.name for parameter in commands["list"].parameters], ["member", "tag"])

    def test_member_accounts_are_sorted_by_name_after_primary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "clan_links.db"
            with patch("elbow_helper.features.account_links.database.DB_PATH", database_path):
                links = _LinksHarness()
                links._init_db()
                links.upsert_link(
                    player_tag="#PRIMARY",
                    discord_user_id=1,
                    is_primary=True,
                    player_name_last_seen="Zulu",
                )
                links.upsert_link(
                    player_tag="#ZULU",
                    discord_user_id=1,
                    player_name_last_seen="Zulu",
                )
                links.upsert_link(
                    player_tag="#ALPHA",
                    discord_user_id=1,
                    player_name_last_seen="Alpha",
                )

                rows = links.get_links_for_user(1)

        self.assertEqual([row["player_tag"] for row in rows], ["#PRIMARY", "#ALPHA", "#ZULU"])


class ClanSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_location_is_exposed_as_a_snapshot_copy(self) -> None:
        links = object.__new__(AccountLinks)
        links._player_locations = {
            "#PLAYER": {
                "player_name": "Ahmad",
                "clan_code": "BEH",
                "townhall": 18,
            }
        }

        location = links.get_player_location("#PLAYER")

        self.assertEqual(location["clan_code"], "BEH")
        location["clan_code"] = "MFG"
        self.assertEqual(links._player_locations["#PLAYER"]["clan_code"], "BEH")
        self.assertIsNone(links.get_player_location("#MISSING"))

    async def test_clan_snapshot_keeps_the_live_badge_url(self) -> None:
        links = object.__new__(AccountLinks)
        links.clash_client = SimpleNamespace(configured=True)
        links._clan_badge_urls = {}
        links._fetch_coc_json = AsyncMock(
            return_value={
                "badgeUrls": {
                    "small": "https://example.com/beh-small.png",
                    "large": "https://example.com/beh-large.png",
                },
                "memberList": [],
            }
        )

        await links._fetch_clan_members("BEH")

        self.assertEqual(
            links.get_clan_badge_url("BEH"),
            "https://example.com/beh-small.png",
        )
