"""Role filters use current complete guild membership."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.research.members import read_discord_members


class MemberRoleTests(unittest.IsolatedAsyncioTestCase):
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
