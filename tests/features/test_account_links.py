from __future__ import annotations

import sqlite3
from contextlib import contextmanager
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from elbow_helper.features.account_links.database import AccountLinksDbMixin
from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.account_links.evidence import (
    account_ownership_evidence, account_tag_evidence,
)


class _LinksHarness(AccountLinksDbMixin):
    pass


class AccountLinksTests(unittest.TestCase):
    def test_current_ownership_evidence_keeps_unlinked_accounts(self):
        source = SimpleNamespace(get_links_by_tags=lambda tags: {
            tag: ({"discord_user_id": 42, "player_name_last_seen": "Alpha"}
                  if tag == "#P0" else None)
            for tag in tags
        })
        snapshot = account_ownership_evidence(source, ("#P0", "#P2", "#P0"))
        self.assertEqual([row.player_tag for row in snapshot.accounts], ["#P0", "#P2"])
        self.assertEqual(snapshot.accounts[0].linked_member_id, 42)
        self.assertIsNone(snapshot.accounts[1].linked_member_id)
        self.assertIsNotNone(snapshot.observed_at)

    def test_tag_group_query_returns_missing_tags_and_one_ownership_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("elbow_helper.features.account_links.database.DB_PATH", Path(directory) / "links.db"):
                links = _LinksHarness()
                links._init_db()
                links.upsert_link(player_tag="#P0", discord_user_id=1)
                result = links.get_links_by_tags(("#P0", "#P2", "#P0"))
        self.assertEqual(tuple(result), ("#P0", "#P2"))
        self.assertEqual(result["#P0"]["discord_user_id"], 1)
        self.assertIsNone(result["#P2"])

    def test_tag_group_query_cannot_mix_ownership_reassigned_between_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("elbow_helper.features.account_links.database.DB_PATH", Path(directory) / "links.db"):
                links = _LinksHarness()
                links._init_db()
                links.upsert_link(player_tag="#P0", discord_user_id=1)
                writer = _LinksHarness()
                original_connect = links._db_connect
                batches = []

                @contextmanager
                def connect():
                    with original_connect() as connection:
                        def trace(sql):
                            if sql.startswith("SELECT * FROM links WHERE player_tag IN"):
                                batches.append(sql)
                                if len(batches) == 2:
                                    writer.upsert_link(player_tag="#P0", discord_user_id=2)
                        connection.set_trace_callback(trace)
                        yield connection

                tags = ("#P0", *(f"#MISSING{index}" for index in range(500)))
                with patch.object(links, "_db_connect", connect):
                    result = links.get_links_by_tags(tags)
                self.assertEqual(len(batches), 2)
                self.assertEqual(result["#P0"]["discord_user_id"], 1)
                self.assertEqual(links.get_link_by_tag("#P0")["discord_user_id"], 2)

    def test_group_query_cannot_duplicate_an_account_reassigned_between_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("elbow_helper.features.account_links.database.DB_PATH", Path(directory) / "links.db"):
                links = _LinksHarness()
                links._init_db()
                links.upsert_link(player_tag="#2PP", discord_user_id=1)
                writer = _LinksHarness()
                original_connect = links._db_connect
                batches = []

                @contextmanager
                def connect():
                    with original_connect() as connection:
                        def trace(sql):
                            if sql.startswith("SELECT * FROM links WHERE discord_user_id IN"):
                                batches.append(sql)
                                if len(batches) == 2:
                                    writer.upsert_link(player_tag="#2PP", discord_user_id=501)
                        connection.set_trace_callback(trace)
                        yield connection

                with patch.object(links, "_db_connect", connect):
                    result = links.get_links_for_members(range(1, 503))
                self.assertEqual(len(batches), 2)
                self.assertEqual([row["player_tag"] for row in result[1]], ["#2PP"])
                self.assertEqual(result[501], [])
                self.assertEqual(links.get_link_by_tag("#2PP")["discord_user_id"], 501)

    def test_group_link_query_includes_unlinked_members_and_crosses_sql_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("elbow_helper.features.account_links.database.DB_PATH", Path(directory) / "links.db"):
                links = _LinksHarness()
                links._init_db()
                links.upsert_link(player_tag="#2PP", discord_user_id=1)
                links.upsert_link(player_tag="#2PQ", discord_user_id=501)
                links.upsert_link(player_tag="#2PR", discord_user_id=999)
                result = links.get_links_for_members(range(1, 503))
        self.assertEqual(len(result), 502)
        self.assertEqual(result[1][0]["player_tag"], "#2PP")
        self.assertEqual(result[501][0]["player_tag"], "#2PQ")
        self.assertEqual(result[2], [])
        self.assertNotIn(999, result)
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

    def test_batch_upsert_rolls_back_every_link_when_one_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "clan_links.db"
            with patch("elbow_helper.features.account_links.database.DB_PATH", database_path):
                links = _LinksHarness()
                links._init_db()
                with links._db_connect() as conn:
                    conn.execute(
                        """
                        CREATE TRIGGER reject_test_link
                        BEFORE INSERT ON links
                        WHEN NEW.player_tag = '#FAIL'
                        BEGIN
                            SELECT RAISE(ABORT, 'rejected test link');
                        END
                        """
                    )
                    conn.commit()

                with self.assertRaises(sqlite3.IntegrityError):
                    links.upsert_links(
                        [
                            {"player_tag": "#FIRST", "discord_user_id": 1},
                            {"player_tag": "#FAIL", "discord_user_id": 1},
                        ]
                    )

                self.assertIsNone(links.get_link_by_tag("#FIRST"))
                self.assertIsNone(links.get_link_by_tag("#FAIL"))

    def test_batch_delete_rolls_back_every_link_when_one_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "clan_links.db"
            with patch("elbow_helper.features.account_links.database.DB_PATH", database_path):
                links = _LinksHarness()
                links._init_db()
                links.upsert_links(
                    [
                        {"player_tag": "#FIRST", "discord_user_id": 1},
                        {"player_tag": "#FAIL", "discord_user_id": 1},
                    ]
                )
                with links._db_connect() as conn:
                    conn.execute(
                        """
                        CREATE TRIGGER reject_test_delete
                        BEFORE DELETE ON links
                        WHEN OLD.player_tag = '#FAIL'
                        BEGIN
                            SELECT RAISE(ABORT, 'rejected test delete');
                        END
                        """
                    )
                    conn.commit()

                with self.assertRaises(sqlite3.IntegrityError):
                    links.delete_links(["#FIRST", "#FAIL"])

                self.assertIsNotNone(links.get_link_by_tag("#FIRST"))
                self.assertIsNotNone(links.get_link_by_tag("#FAIL"))


class AccountLinkCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_keeps_saved_result_when_board_refresh_fails(self) -> None:
        links = object.__new__(AccountLinks)
        links._can_manage_accounts = MagicMock(return_value=True)
        links._parse_player_tag_input = MagicMock(return_value=(["#P0Y", "#LQG"], []))
        links.lookup_players = AsyncMock(
            return_value=[
                {"player_tag": "#P0Y", "player_name": "Alpha"},
                {"player_tag": "#LQG", "player_name": "Bravo"},
            ]
        )
        links.get_links_for_user = MagicMock(return_value=[])
        links.get_all_links = MagicMock(return_value={})
        links.upsert_links = MagicMock()
        links._try_refresh_linked_boards = AsyncMock()

        interaction = SimpleNamespace(
            user=SimpleNamespace(),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        member = SimpleNamespace(id=42, mention="<@42>")

        await AccountLinks.account_add.callback(links, interaction, member, "#P0Y #LQG")

        batch = links.upsert_links.call_args.args[0]
        self.assertEqual([row["player_tag"] for row in batch], ["#P0Y", "#LQG"])
        message = interaction.followup.send.call_args.args[0]
        self.assertIn("Linked Alpha (`#P0Y`) to <@42>", message)
        self.assertIn("Linked Bravo (`#LQG`) to <@42>", message)
        self.assertNotIn("Missing Elder Rank", message)

    async def test_board_refresh_failure_is_logged_and_contained(self) -> None:
        links = object.__new__(AccountLinks)
        links.refresh_linked_boards = AsyncMock(side_effect=RuntimeError("refresh failed"))

        with self.assertLogs("elbow_helper.features.account_links.cog", level="ERROR") as captured:
            await links._try_refresh_linked_boards()

        self.assertIn("Failed to refresh Missing Elder Rank boards", captured.output[0])


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

    async def test_group_location_snapshot_is_copied_with_coverage(self) -> None:
        links = object.__new__(AccountLinks)
        links._player_locations = {"#P0": {"player_name": "Alpha", "clan_code": "BEH"}}
        links._last_snapshot_complete = True
        links._location_snapshot_observed_at = "2026-09-17T12:00:00+00:00"
        result = links.get_player_locations_snapshot(("#P0", "#P2"))
        self.assertTrue(result["complete"])
        self.assertEqual(result["observed_at"], "2026-09-17T12:00:00+00:00")
        self.assertEqual(tuple(result["locations"]), ("#P0",))
        result["locations"]["#P0"]["clan_code"] = "BEC"
        self.assertEqual(links._player_locations["#P0"]["clan_code"], "BEH")

    async def test_account_tag_evidence_distinguishes_absent_complete_location(self) -> None:
        source = SimpleNamespace(
            get_links_by_tags=MagicMock(return_value={
                "#P0": {"discord_user_id": 42, "player_name_last_seen": "Alpha", "last_seen_clan_code": "BEH"},
                "#P2": None,
            }),
            get_player_locations_snapshot=MagicMock(return_value={
                "observed_at": "now", "complete": True,
                "locations": {"#P0": {"player_name": "Alpha", "clan_code": "BEH", "clan_tag": "#TAG", "townhall": 18}},
            }),
        )
        snapshot = account_tag_evidence(source, ("#P0", "#P2"))
        self.assertEqual(snapshot.accounts[0].linked_member_id, 42)
        self.assertEqual(snapshot.accounts[0].location_status, "observed_family_clan")
        self.assertEqual(snapshot.accounts[1].location_status, "not_in_complete_family_snapshot")

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
                "members": 0,
                "memberList": [],
            }
        )

        await links._fetch_clan_members("BEH")

        self.assertEqual(
            links.get_clan_badge_url("BEH"),
            "https://example.com/beh-small.png",
        )

    async def test_incomplete_clan_response_is_rejected(self) -> None:
        links = object.__new__(AccountLinks)
        links.clash_client = SimpleNamespace(configured=True)
        links._clan_badge_urls = {}
        links._fetch_coc_json = AsyncMock(
            return_value={
                "members": 2,
                "memberList": [
                    {"tag": "#ONE", "name": "One"},
                ],
            }
        )

        with self.assertLogs(
            "elbow_helper.features.account_links.cog",
            level="WARNING",
        ):
            members = await links._fetch_clan_members("BEH")

        self.assertIsNone(members)

    async def test_incomplete_clan_response_preserves_the_previous_snapshot(self) -> None:
        links = object.__new__(AccountLinks)
        links.clash_client = SimpleNamespace(configured=True)
        known_member = {
            "player_tag": "#KNOWN",
            "player_name": "Known",
            "clan_code": "BEH",
        }
        links._clan_members = {"BEH": {"#KNOWN": known_member}}
        links._player_locations = {"#KNOWN": known_member}
        links._fetch_clan_members = AsyncMock(return_value=None)

        with patch(
            "elbow_helper.features.account_links.cog.TRACKED_CLAN_CODES",
            ("BEH",),
        ):
            await links._rebuild_snapshots()

        self.assertFalse(links._last_snapshot_complete)
        self.assertEqual(links._clan_members["BEH"], {"#KNOWN": known_member})
        self.assertEqual(links._player_locations, {"#KNOWN": known_member})
