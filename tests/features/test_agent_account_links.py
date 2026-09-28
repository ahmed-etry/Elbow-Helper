from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.models import AgentRequestContext, AgentTurnState
from elbow_helper.features.agent.tools import build_agent_tools


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

        self.assertEqual(result["total_linked_accounts"], 26)
        self.assertTrue(result["accounts_truncated"])
        self.assertEqual(len(result["accounts"]), 25)
        self.assertEqual(result["accounts"][0]["observed_clan_code"], "BE4")
        self.assertEqual(result["accounts"][0]["last_seen_clan_code"], "BEH")
        self.assertIsNone(result["accounts"][1]["observed_clan_code"])
        self.assertEqual(result["accounts"][1]["location_status"], "location_unknown")
