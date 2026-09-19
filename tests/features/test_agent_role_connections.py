from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from elbow_helper.configuration.roles import CORE, LEAD
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.tools import build_agent_tools
from elbow_helper.features.agent.tools.role_connections import read_role_connections
from elbow_helper.features.role_connections.queries import RoleConnectionQueries


class AgentRoleConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        core_id = next(iter(CORE))
        lead_id = next(iter(LEAD))
        role_ids = {10, 20, 30, core_id, lead_id}
        self.roles = {
            role_id: SimpleNamespace(id=role_id, name=f"Role {role_id}")
            for role_id in role_ids
        }
        self.requester = SimpleNamespace(
            id=1, display_name="Requester",
            roles=[self.roles[core_id], self.roles[lead_id]],
        )
        self.target = SimpleNamespace(
            id=42, display_name="Target", roles=[self.roles[20]],
        )
        self.bot_member = SimpleNamespace(id=99, roles=[])
        members = {
            self.requester.id: self.requester,
            self.target.id: self.target,
            self.bot_member.id: self.bot_member,
        }
        self.guild = SimpleNamespace(
            id=100, me=self.bot_member, get_member=members.get,
            get_role=self.roles.get,
        )
        self.channel = SimpleNamespace(
            id=200, guild=self.guild,
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, read_message_history=True,
            ),
        )
        self.guild.get_channel_or_thread = lambda channel_id: (
            self.channel if channel_id == self.channel.id else None
        )
        self.state = [
            {
                "id": "add-ten", "target_role_id": 10,
                "all": [{"has": 20}], "any": [],
            },
            {
                "id": "remove-thirty", "target_role_id": 30,
                "all": [{"not": 20}], "any": [],
            },
            {"id": "broken", "target_role_id": False},
        ]
        self.context = AgentRequestContext(
            bot=SimpleNamespace(), guild=self.guild, member=self.requester,
            source_message=SimpleNamespace(
                channel=self.channel, created_at=datetime.now(timezone.utc),
            ),
            account_links=None, clan_health=None, message_search=None,
            role_connection_queries=RoleConnectionQueries(lambda: self.state),
        )

    async def test_reads_and_evaluates_current_member_roles(self):
        result = await read_role_connections(self.context, {
            "member_id": self.target.id, "limit": 1,
        })

        self.assertEqual(result["valid_rules"], 2)
        self.assertEqual(result["malformed_rule_count"], 1)
        self.assertFalse(result["all_entries_valid"])
        self.assertEqual(result["next_offset"], 1)
        self.assertEqual(result["evaluated_member"]["member_id"], 42)
        self.assertTrue(result["rules"][0]["rule_matches_member"])
        self.assertFalse(result["rules"][0]["member_currently_has_target"])
        self.assertEqual(result["rules"][0]["target_role_name"], "Role 10")
        self.assertIn("lead", self.context.state.required_access)

        second = await read_role_connections(self.context, {
            "member_id": self.target.id, "offset": result["next_offset"],
            "expected_state_fingerprint": result["state_fingerprint"],
        })
        self.assertFalse(second["rules"][0]["rule_matches_member"])

    async def test_reads_fresh_roles_and_rejects_changed_page_identity(self):
        first = await read_role_connections(self.context, {
            "member_id": self.target.id,
        })
        self.target.roles = [self.roles[30]]
        refreshed = await read_role_connections(self.context, {
            "member_id": self.target.id,
        })
        self.assertFalse(refreshed["rules"][0]["rule_matches_member"])

        self.state.append({
            "id": "new", "target_role_id": 20, "all": [], "any": [],
        })
        stale = await read_role_connections(self.context, {
            "offset": 1,
            "expected_state_fingerprint": first["state_fingerprint"],
        })
        self.assertIn("changed", stale["error"])

    async def test_current_lead_access_is_required_and_retained(self):
        await read_role_connections(self.context, {})
        self.requester.roles = [
            role for role in self.requester.roles if role.id not in LEAD
        ]
        with self.assertRaises(AgentAccessLost):
            await read_role_connections(self.context, {})

        core_only = SimpleNamespace(
            id=2, display_name="Core",
            roles=[SimpleNamespace(id=next(iter(CORE)))],
        )
        self.guild.get_member = lambda member_id: {
            core_only.id: core_only, self.bot_member.id: self.bot_member,
        }.get(member_id)
        context = AgentRequestContext(
            bot=SimpleNamespace(), guild=self.guild, member=core_only,
            source_message=SimpleNamespace(channel=self.channel),
            account_links=None, clan_health=None, message_search=None,
            role_connection_queries=RoleConnectionQueries(lambda: self.state),
        )
        with self.assertRaises(AgentAccessLost):
            await read_role_connections(context, {})

    async def test_lead_loss_during_snapshot_blocks_the_result(self):
        def revoke_while_reading():
            self.requester.roles = [
                role for role in self.requester.roles if role.id not in LEAD
            ]
            return self.state

        context = replace(
            self.context,
            role_connection_queries=RoleConnectionQueries(revoke_while_reading),
        )
        with self.assertRaises(AgentAccessLost):
            await read_role_connections(context, {})

    async def test_missing_member_and_query_do_not_claim_evidence(self):
        missing_member = await read_role_connections(
            self.context, {"member_id": 404},
        )
        self.assertIn("error", missing_member)
        unavailable = await read_role_connections(
            replace(self.context, role_connection_queries=None), {},
        )
        self.assertIn("error", unavailable)

    def test_tool_is_discoverable_in_members_and_roles_group(self):
        tool = build_agent_tools()["read_role_connections"].definition
        self.assertIn("Requires current Lead access", tool.description)
        self.assertIn("expected_state_fingerprint", tool.parameters["properties"])


if __name__ == "__main__":
    unittest.main()
