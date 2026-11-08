from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
from elbow_helper.agent.engine.registry import build_agent_tools


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
        with patch("elbow_helper.agent.research.members.require_evidence_access",
                   new=AsyncMock()) as access:
            for sorting, expected in (("oldest_first", [202, 404, 101, 303]),
                                      ("newest_first", [101, 202, 404, 303])):
                args = {
                    "member_ids": [303, 101, 404, 202, 999], "sort": sorting,
                    "limit": 2, "include_roles": True,
                }
                first = await handler(context, args)
                second = await handler(context, {**args, "offset": first["next_offset"]})
                self.assertEqual([row["member_id"] for row in first["members"] + second["members"]], expected)
                self.assertEqual(first["total_members"], 4)
                self.assertEqual(first["missing_member_ids"], [999])
                self.assertIsNone(second["next_offset"])
                self.assertEqual(first["members"][0]["roles"], [{"role_id": 9, "name": "Synthetic role"}])
                self.assertIn("+00:00", first["members"][0]["joined_at"])
            self.assertEqual(access.await_count, 8)


    async def test_bulk_member_access_is_rechecked_before_returning(self):
        from elbow_helper.agent.access import AgentAccessLost
        from elbow_helper.agent.research.members import read_discord_members
        with patch("elbow_helper.agent.research.members.require_evidence_access",
                   new=AsyncMock(side_effect=[None, AgentAccessLost()])):
            with self.assertRaises(AgentAccessLost):
                await read_discord_members(self.member_context(), {"member_ids": [101]})
