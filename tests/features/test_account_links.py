from __future__ import annotations

import asyncio

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from tests.data.link_fixtures import attach_links
from elbow_helper.features.account_links.database import AccountLinksDbMixin
from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.account_links.evidence import (
    account_ownership_evidence, account_tag_evidence,
)
from elbow_helper.infrastructure.clash import ClashResponse


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
                asyncio.run(attach_links(links))
                asyncio.run(links.upsert_link(player_tag="#P0", discord_user_id=1))
                result = links.get_links_by_tags(("#P0", "#P2", "#P0"))
        self.assertEqual(tuple(result), ("#P0", "#P2"))
        self.assertEqual(result["#P0"]["discord_user_id"], 1)
        self.assertIsNone(result["#P2"])

    def test_tag_group_query_cannot_mix_ownership_reassigned_between_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("elbow_helper.features.account_links.database.DB_PATH", Path(directory) / "links.db"):
                links = _LinksHarness()
                links._init_db()
                asyncio.run(attach_links(links))
                asyncio.run(links.upsert_link(player_tag="#P0", discord_user_id=1))
                original_read = links.link_service.read_links_in_worker
                reads = []

                def moved_after_snapshot(select=None):
                    rows = original_read(select)
                    reads.append(rows)
                    asyncio.run(links.upsert_link(player_tag="#P0", discord_user_id=2))
                    return rows

                tags = ("#P0", *(f"#MISSING{index}" for index in range(500)))
                with patch.object(links.link_service, "read_links_in_worker", moved_after_snapshot):
                    result = links.get_links_by_tags(tags)
                self.assertEqual(len(reads), 1)
                self.assertEqual(result["#P0"]["discord_user_id"], 1)
                self.assertEqual(links.get_link_by_tag("#P0")["discord_user_id"], 2)

    def test_group_query_cannot_duplicate_an_account_reassigned_between_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("elbow_helper.features.account_links.database.DB_PATH", Path(directory) / "links.db"):
                links = _LinksHarness()
                links._init_db()
                asyncio.run(attach_links(links))
                asyncio.run(links.upsert_link(player_tag="#2PP", discord_user_id=1))
                original_read = links.link_service.read_links_in_worker
                reads = []

                def moved_after_snapshot(select=None):
                    rows = original_read(select)
                    reads.append(rows)
                    asyncio.run(links.upsert_link(player_tag="#2PP", discord_user_id=501))
                    return rows

                with patch.object(links.link_service, "read_links_in_worker", moved_after_snapshot):
                    result = links.get_links_for_members(range(1, 503))
                self.assertEqual(len(reads), 1)
                self.assertEqual([row["player_tag"] for row in result[1]], ["#2PP"])
                self.assertEqual(result[501], [])
                self.assertEqual(links.get_link_by_tag("#2PP")["discord_user_id"], 501)

    def test_group_link_query_includes_unlinked_members_and_crosses_sql_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("elbow_helper.features.account_links.database.DB_PATH", Path(directory) / "links.db"):
                links = _LinksHarness()
                links._init_db()
                asyncio.run(attach_links(links))
                asyncio.run(links.upsert_link(player_tag="#2PP", discord_user_id=1))
                asyncio.run(links.upsert_link(player_tag="#2PQ", discord_user_id=501))
                asyncio.run(links.upsert_link(player_tag="#2PR", discord_user_id=999))
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

    def test_member_accounts_are_sorted_by_name_then_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "clan_links.db"
            with patch("elbow_helper.features.account_links.database.DB_PATH", database_path):
                links = _LinksHarness()
                links._init_db()
                asyncio.run(attach_links(links))
                asyncio.run(links.upsert_link(
                    player_tag="#P0",
                    discord_user_id=1,
                    player_name_last_seen="Zulu",
                ))
                asyncio.run(links.upsert_link(
                    player_tag="#V0",
                    discord_user_id=1,
                    player_name_last_seen="Zulu",
                ))
                asyncio.run(links.upsert_link(
                    player_tag="#R0",
                    discord_user_id=1,
                    player_name_last_seen="Alpha",
                ))

                rows = links.get_links_for_user(1)

        self.assertEqual([row["player_tag"] for row in rows], ["#R0", "#P0", "#V0"])

    def test_batch_upsert_rolls_back_every_link_when_one_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "clan_links.db"
            with patch("elbow_helper.features.account_links.database.DB_PATH", database_path):
                links = _LinksHarness()
                links._init_db()
                asyncio.run(attach_links(links))
                with links._db_connect() as conn:
                    conn.execute(
                        """
                        CREATE TRIGGER reject_test_link
                        BEFORE INSERT ON links
                        WHEN NEW.player_tag = '#Q0'
                        BEGIN
                            SELECT RAISE(ABORT, 'rejected test link');
                        END
                        """
                    )
                    conn.commit()

                with self.assertRaises(sqlite3.IntegrityError):
                    asyncio.run(links.upsert_links(
                        [
                            {"player_tag": "#P0", "discord_user_id": 1},
                            {"player_tag": "#Q0", "discord_user_id": 1},
                        ]
                    ))

                self.assertIsNone(links.get_link_by_tag("#P0"))
                self.assertIsNone(links.get_link_by_tag("#Q0"))

    def test_batch_delete_rolls_back_every_link_when_one_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "clan_links.db"
            with patch("elbow_helper.features.account_links.database.DB_PATH", database_path):
                links = _LinksHarness()
                links._init_db()
                asyncio.run(attach_links(links))
                asyncio.run(links.upsert_links(
                    [
                        {"player_tag": "#P0", "discord_user_id": 1},
                        {"player_tag": "#Q0", "discord_user_id": 1},
                    ]
                ))
                with links._db_connect() as conn:
                    conn.execute(
                        """
                        CREATE TRIGGER reject_test_delete
                        BEFORE DELETE ON links
                        WHEN OLD.player_tag = '#Q0'
                        BEGIN
                            SELECT RAISE(ABORT, 'rejected test delete');
                        END
                        """
                    )
                    conn.commit()

                with self.assertRaises(sqlite3.IntegrityError):
                    asyncio.run(links.delete_links(["#P0", "#Q0"]))

                self.assertIsNotNone(links.get_link_by_tag("#P0"))
                self.assertIsNotNone(links.get_link_by_tag("#Q0"))


class AccountLinkCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_rejects_404_and_saves_temporary_failures_and_named_players(self) -> None:
        links = object.__new__(AccountLinks)
        tags = ["#P0Y", "#LQG", "#2PP", "#2PQ", "#2PR"]
        links.clash_client = SimpleNamespace(
            configured=True,
            get=AsyncMock(side_effect=[
                ClashResponse(404, {"reason": "notFound"}, {}, 1, 0),
                ClashResponse(None, None, {}, 3, 0, TimeoutError()),
                ClashResponse(503, {"reason": "maintenance"}, {}, 3, 0),
                ClashResponse(429, {"reason": "requestThrottled"}, {}, 3, 0),
                ClashResponse(200, {"name": "Alpha"}, {}, 1, 0),
            ]),
        )
        links._can_manage_accounts = MagicMock(return_value=True)
        links._try_refresh_linked_boards = AsyncMock()
        interaction = SimpleNamespace(
            user=SimpleNamespace(),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        member = SimpleNamespace(id=42, mention="<@42>")

        with tempfile.TemporaryDirectory() as directory:
            with patch("elbow_helper.features.account_links.database.DB_PATH", Path(directory) / "links.db"):
                links._init_db()
                await attach_links(links)
                await AccountLinks.account_add.callback(
                    links, interaction, member, " ".join([*tags, "bad"]),
                )
                saved = (await asyncio.to_thread(links.get_all_links))

        self.assertEqual(set(saved), set(tags[1:]))
        self.assertTrue(all(row["discord_user_id"] == 42 for row in saved.values()))
        for tag in tags[1:4]:
            self.assertEqual(saved[tag]["player_name_last_seen"], tag)
        self.assertEqual(saved[tags[4]]["player_name_last_seen"], "Alpha")
        message = interaction.followup.send.await_args.args[0]
        self.assertIn("- Invalid player tags: bad, #P0Y", message)
        self.assertNotIn("Linked #P0Y", message)
        self.assertIn("- Linked Alpha (`#2PR`) to <@42>", message)
        self.assertEqual(
            [call.args[0] for call in links.clash_client.get.await_args_list],
            [f"/players/%23{tag[1:]}" for tag in tags],
        )

    async def test_add_only_404_tags_are_excluded_from_writes_and_undo(self) -> None:
        for commit in (False, True):
            with self.subTest(commit=commit):
                links = object.__new__(AccountLinks)
                links.clash_client = SimpleNamespace(
                    configured=True,
                    get=AsyncMock(return_value=ClashResponse(404, None, {}, 1, 0)),
                )
                links.get_links_for_user = MagicMock(return_value=[])
                links.get_all_links = MagicMock(return_value={"#P0Y": {"discord_user_id": 99}})
                links.upsert_links = AsyncMock()
                links._try_refresh_linked_boards = AsyncMock()

                result = await links.account_add_operation(
                    SimpleNamespace(id=42, mention="<@42>"), "#P0Y", commit=commit,
                )

                self.assertIsNone(result["error"])
                self.assertEqual(result["message"], "- Invalid player tags: #P0Y")
                self.assertEqual(result["rows"], [])
                self.assertEqual(result["before"], {})
                links.upsert_links.assert_not_called()
                links._try_refresh_linked_boards.assert_not_awaited()

    async def test_add_without_clash_setup_still_saves_the_tag(self) -> None:
        links = object.__new__(AccountLinks)
        links.clash_client = SimpleNamespace(configured=False, get=AsyncMock())
        links._try_refresh_linked_boards = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            with patch("elbow_helper.features.account_links.database.DB_PATH", Path(directory) / "links.db"):
                links._init_db()
                await attach_links(links)
                result = await links.account_add_operation(
                    SimpleNamespace(id=42, mention="<@42>"), "#P0Y",
                )
                self.assertEqual((await asyncio.to_thread(links.get_link_by_tag, "#P0Y"))["player_name_last_seen"], "#P0Y")
        self.assertEqual(result["invalid_tags"], [])
        links.clash_client.get.assert_not_awaited()

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
        links.upsert_links = AsyncMock()
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
        self.assertTrue(all("is_primary" not in row for row in batch))
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
