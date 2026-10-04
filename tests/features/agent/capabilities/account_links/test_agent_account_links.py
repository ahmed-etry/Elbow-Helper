from types import SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timedelta, timezone

from features.agent.result_path_helpers import assert_result_paths

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.engine.registry import build_agent_tools


class _Channel:
    def __init__(self, channel_id, name, visible_members):
        self.id = channel_id
        self.name = name
        self.guild = None
        self.visible_members = visible_members

    def permissions_for(self, actor):
        visible = actor.id in self.visible_members
        return SimpleNamespace(view_channel=visible, read_message_history=visible)


class AgentAccountLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_account_link_keeps_link_and_location_evidence_distinct(self):
        member = SimpleNamespace(
            id=42, roles=[SimpleNamespace(id=next(iter(CORE)))],
            display_name="Owner",
        )
        bot_member = SimpleNamespace(id=20)
        channel = _Channel(100, "chat", {42, 20})
        guild = SimpleNamespace(
            id=1, me=bot_member,
            get_member=lambda member_id: member if member_id == 42 else None,
        )
        channel.guild = guild
        guild.get_channel_or_thread = lambda channel_id: channel if channel_id == 100 else None
        account_links = SimpleNamespace(
            get_link_by_tag=Mock(return_value={
                "discord_user_id": 42, "is_primary": 1,
                "player_name_last_seen": "Older name",
                "last_seen_clan_code": "BEH", "last_seen_clan_tag": "#OLD",
                "last_seen_role": "member",
            }),
            get_player_locations_snapshot=Mock(return_value={
                "observed_at": "2026-09-25T10:00:00+00:00", "complete": True,
                "locations": {"#P0": {
                    "player_name": "Current name", "clan_code": "BEC",
                    "clan_tag": "#NOW", "role": "elder",
                }},
                "location_statuses": {"#P0": "observed_family_clan"},
            }),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(), guild=guild, member=member,
            source_message=SimpleNamespace(channel=channel),
            account_links=account_links,
            state=AgentTurnState(source_channels={100}),
        )
        handler = build_agent_tools()["get_account_link"].handler

        linked = await handler(context, {"player_tag": "p0"})

        account_links.get_link_by_tag.assert_called_once_with("#P0")
        account_links.get_player_locations_snapshot.assert_called_once_with(("#P0",))
        assert_result_paths(self, "get_account_link", linked)
        self.assertEqual(linked["linked_member_id"], 42)
        self.assertEqual(linked["linked_member_display_name"], "Owner")
        self.assertTrue(linked["primary"])
        self.assertEqual(linked["last_seen_clan_code"], "BEH")
        self.assertEqual(linked["observed_clan_code"], "BEC")
        self.assertEqual(linked["location_snapshot_observed_at"],
                         "2026-09-25T10:00:00+00:00")

        account_links.get_link_by_tag.return_value = None
        account_links.get_player_locations_snapshot.return_value = {
            "observed_at": None, "complete": False,
            "locations": {}, "location_statuses": {},
        }
        unlinked = await handler(context, {"player_tag": "#P2"})
        self.assertEqual(unlinked["link_status"], "unlinked")
        self.assertIsNone(unlinked["primary"])
        self.assertIsNone(unlinked["linked_member_id"])
        self.assertEqual(unlinked["location_status"], "location_unknown")


    async def test_linked_account_result_keeps_observed_and_last_seen_locations_distinct(self):
        member = SimpleNamespace(
            id=10, roles=[SimpleNamespace(id=next(iter(CORE)))],
            display_name="Member",
        )
        bot_member = SimpleNamespace(id=20)
        channel = _Channel(100, "chat", {10, 20})
        guild = SimpleNamespace(
            id=1, me=bot_member,
            get_member=lambda member_id: member if member_id == 10 else None,
        )
        channel.guild = guild
        guild.get_channel_or_thread = lambda channel_id: channel if channel_id == 100 else None
        rows = [
            {"player_tag": f"#TAG{index}", "is_primary": index == 0,
             "player_name_last_seen": f"Old {index}",
             "last_seen_clan_code": "BEH", "last_seen_clan_tag": "#OLD",
             "last_seen_role": "member"}
            for index in range(26)
        ]
        account_links = SimpleNamespace(
            get_links_for_user=lambda member_id: rows,
            get_player_locations_snapshot=lambda tags: {
                "observed_at": "2026-09-24T10:00:00+00:00", "complete": False,
                "locations": {"#TAG0": {
                    "player_name": "Observed", "clan_code": "BE4",
                    "clan_tag": "#NOW", "role": "elder",
                }},
                "location_statuses": {"#TAG0": "observed_family_clan"},
            },
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(), guild=guild, member=member,
            source_message=SimpleNamespace(channel=channel),
            account_links=account_links,
            state=AgentTurnState(source_channels={100}),
        )

        result = await build_agent_tools()["get_linked_accounts"].handler(
            context, {"member_id": 10},
        )

        assert_result_paths(self, "get_linked_accounts", result)
        self.assertEqual(result["total_linked_accounts"], 26)
        self.assertTrue(result["accounts_truncated"])
        self.assertEqual(len(result["accounts"]), 25)
        self.assertEqual(result["accounts"][0]["observed_clan_code"], "BE4")
        self.assertEqual(result["accounts"][0]["last_seen_clan_code"], "BEH")
        self.assertIsNone(result["accounts"][1]["observed_clan_code"])
        self.assertEqual(result["accounts"][1]["location_status"], "location_unknown")

    async def test_member_discovery_exposes_declared_identity(self):
        member = SimpleNamespace(id=101, display_name="Synthetic member", name="synthetic",
                                 global_name=None, mention="<@101>", roles=[],
                                 joined_at=datetime(2026, 1, 2, 12, tzinfo=timezone(timedelta(hours=2))))
        context = SimpleNamespace(guild=SimpleNamespace(members=[member]))
        result = await build_agent_tools()["find_discord_members"].handler(context, {"query": "synthetic"})
        assert_result_paths(self, "find_discord_members", result)
        self.assertEqual(result["members"][0]["member_id"], 101)
        self.assertEqual(result["members"][0]["joined_at"], "2026-01-02T10:00:00+00:00")

    def member_context(self):
        default = SimpleNamespace(id=1, name="Synthetic default", is_default=lambda: True)
        role = SimpleNamespace(id=9, name="Synthetic role", is_default=lambda: False)
        members = [SimpleNamespace(id=identifier, display_name=f"Synthetic member {identifier}",
                                  name=f"synthetic_{identifier}", roles=[default, role], joined_at=joined)
                   for identifier, joined in (
                       (101, datetime(2026, 1, 3, tzinfo=timezone.utc)),
                       (202, datetime(2026, 1, 1, tzinfo=timezone.utc)),
                       (303, None),
                       (404, datetime(2026, 1, 1, tzinfo=timezone.utc)),
                   )]
        lookup = {member.id: member for member in members}
        return SimpleNamespace(guild=SimpleNamespace(get_member=lookup.get))

    async def test_bulk_member_details_sort_before_limit_and_page_without_duplicates(self):
        handler = build_agent_tools()["read_discord_members"].handler
        context = self.member_context()
        with patch("elbow_helper.features.agent.capabilities.account_links.reads.require_evidence_access",
                   new=AsyncMock()) as access:
            for sorting, expected in (("oldest_first", [202, 404, 101, 303]),
                                      ("newest_first", [101, 202, 404, 303])):
                args = {"member_ids": [303, 101, 404, 202, 999], "sort": sorting, "limit": 2}
                first = await handler(context, args)
                second = await handler(context, {**args, "offset": first["next_offset"]})
                assert_result_paths(self, "read_discord_members", first)
                assert_result_paths(self, "read_discord_members", second)
                self.assertEqual([row["member_id"] for row in first["members"] + second["members"]], expected)
                self.assertEqual(first["total_members"], 4)
                self.assertEqual(first["missing_member_ids"], [999])
                self.assertIsNone(second["next_offset"])
                self.assertEqual(first["members"][0]["roles"], [{"role_id": 9, "name": "Synthetic role"}])
                self.assertIn("+00:00", first["members"][0]["joined_at"])
            self.assertEqual(access.await_count, 8)

    async def test_bulk_member_details_preserve_requested_order_and_unknown_dates(self):
        handler = build_agent_tools()["read_discord_members"].handler
        with patch("elbow_helper.features.agent.capabilities.account_links.reads.require_evidence_access",
                   new=AsyncMock()):
            result = await handler(self.member_context(), {"member_ids": [303, 101], "limit": 1})
            self.assertEqual(result["members"][0]["member_id"], 303)
            self.assertIsNone(result["members"][0]["joined_at"])
            self.assertEqual(result["next_offset"], 1)
            empty = await handler(self.member_context(), {"member_ids": [999]})
            self.assertEqual(empty["members"], [])
            self.assertEqual(empty["missing_member_ids"], [999])
            self.assertIsNone(empty["next_offset"])

    async def test_bulk_member_details_enforce_input_and_output_bounds(self):
        from elbow_helper.features.agent.capabilities.account_links.reads import read_discord_members
        context = self.member_context()
        for args in ({"member_ids": []}, {"member_ids": list(range(1, 1002))},
                     {"member_ids": [101, 101]}, {"member_ids": [True]},
                     {"member_ids": [0]}, {"member_ids": ["101"]},
                     {"member_ids": [101], "offset": -1}, {"member_ids": [101], "offset": 1001},
                     {"member_ids": [101], "limit": 0}, {"member_ids": [101], "limit": 101},
                     {"member_ids": [101], "sort": "unsupported"}):
            with self.subTest(args=args):
                result = await read_discord_members(context, args)
                self.assertIn("error", result)
        with patch("elbow_helper.features.agent.capabilities.account_links.reads.require_evidence_access",
                   new=AsyncMock()):
            context.guild.get_member = lambda identifier: SimpleNamespace(
                id=identifier, name="synthetic", display_name="Synthetic", roles=[],
                joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
            result = await read_discord_members(context, {"member_ids": list(range(1, 101)), "limit": 100})
            self.assertEqual(len(result["members"]), 100)
            self.assertIsNone(result["next_offset"])

    async def test_bulk_member_access_is_rechecked_before_returning(self):
        from elbow_helper.features.agent.access import AgentAccessLost
        from elbow_helper.features.agent.capabilities.account_links.reads import read_discord_members
        with patch("elbow_helper.features.agent.capabilities.account_links.reads.require_evidence_access",
                   new=AsyncMock(side_effect=[None, AgentAccessLost()])):
            with self.assertRaises(AgentAccessLost):
                await read_discord_members(self.member_context(), {"member_ids": [101]})
