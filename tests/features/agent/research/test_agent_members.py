"""Role filters use current complete guild membership."""
from types import SimpleNamespace
from datetime import datetime
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.research.members import read_discord_members


class MemberRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_window_filters_complete_membership_before_sort_and_page(self):
        members = [SimpleNamespace(id=i, display_name=f"Synthetic {i}", name="synthetic",
                   bot=False, roles=[], joined_at=(datetime.fromisoformat(date) if date else None))
                   for i, date in enumerate(("2026-09-30T23:59:59+00:00", "2026-10-01T00:00:00+00:00",
                       "2026-10-20T12:00:00+00:00", "2026-11-01T00:00:00+00:00", None), 1)]
        context = SimpleNamespace(guild=SimpleNamespace(chunked=True, members=members,
                                  get_member=lambda i: members[i - 1]))
        with patch("elbow_helper.features.agent.research.members.require_evidence_access", AsyncMock()):
            result = await read_discord_members(context, {
                "joined_after": "2026-10-01T02:00:00+02:00", "joined_before": "2026-11-01T00:00:00Z",
                "sort": "newest_first", "limit": 1})
            self.assertEqual(result["total_members"], 2)
            self.assertEqual(result["members"][0]["member_id"], 3)
            self.assertEqual(result["next_offset"], 1)
            # A hibernating cohort uses its IDs, then sorts by Discord joins.
            cohort = await read_discord_members(context, {
                "member_ids": [3, 2, 5], "sort": "oldest_first", "limit": 10})
            self.assertEqual([m["member_id"] for m in cohort["members"]], [2, 3, 5])

    async def test_invalid_join_window_never_fetches_members(self):
        context = SimpleNamespace(guild=SimpleNamespace(chunk=AsyncMock()))
        for arguments in ({"joined_after": "not a date"}, {"joined_after": "2026-10-01"},
                          {"joined_after": "2026-11-01T00:00:00Z", "joined_before": "2026-10-01T00:00:00Z"}):
            result = await read_discord_members(context, arguments)
            self.assertEqual(result["cause"], "invalid_arguments")
        context.guild.chunk.assert_not_awaited()

    async def test_server_wide_paging_can_continue_after_one_thousand_members(self):
        members = [SimpleNamespace(id=i, display_name=f"Synthetic {i}", name="synthetic",
                                   bot=False, roles=[], joined_at=None) for i in range(1, 1003)]
        context = SimpleNamespace(guild=SimpleNamespace(chunked=True, members=members,
                                  get_member=lambda i: members[i - 1]))
        with patch("elbow_helper.features.agent.research.members.require_evidence_access", AsyncMock()):
            result = await read_discord_members(context, {"offset": 1001, "limit": 1, "sort": "oldest_first"})
        self.assertEqual(result["members"][0]["member_id"], 1002)
        self.assertIsNone(result["next_offset"])

    async def test_role_any_all_and_member_intersection(self):
        def member(identifier, roles):
            return SimpleNamespace(
                id=identifier, display_name=f"Synthetic {identifier}", name="synthetic",
                bot=False, joined_at=None,
                roles=[
                    SimpleNamespace(id=role, name=f"Role {role}", is_default=lambda: False)
                    for role in roles
                ],
            )

        members = [member(1, [10]), member(2, [10, 20]), member(3, [20])]
        context = SimpleNamespace(guild=SimpleNamespace(
            chunked=True, members=members,
            get_member=lambda identifier: next((m for m in members if m.id == identifier), None),
        ))
        with patch(
            "elbow_helper.features.agent.research.members.require_evidence_access", AsyncMock(),
        ):
            for values, expected in (
                ({"role_ids": [10, 20]}, [1, 2, 3]),
                ({"role_ids": [10, 20], "role_match": "all"}, [2]),
                ({"role_ids": [10], "member_ids": [2, 3]}, [2]),
            ):
                result = await read_discord_members(context, values)
                self.assertEqual([row["member_id"] for row in result["members"]], expected)
                self.assertNotIn("roles", result["members"][0])
                self.assertFalse(result["members"][0]["bot"])
            result = await read_discord_members(context, {"role_ids": [10], "include_roles": True})
            self.assertEqual(result["members"][0]["roles"], [{"role_id": 10, "name": "Role 10"}])
