"""Saved limits apply to checked capability arguments across prepared actions."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.discord_actions.roles import prepare_add_roles
from elbow_helper.features.agent.discord_actions.messages import prepare_post
from elbow_helper.features.agent.scheduled.scope import validate_scope, within_scope


class CheckedActionScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        role = SimpleNamespace(id=12, mention="<@&12>", position=1, managed=False,
                               permissions=SimpleNamespace(), is_default=lambda: False)
        members = {identifier: SimpleNamespace(id=identifier, mention=f"<@{identifier}>",
                    roles=[], top_role=SimpleNamespace(position=1)) for identifier in (4, 5, 6)}
        channel = SimpleNamespace(id=20, mention="<#20>", guild=SimpleNamespace(id=1),
            permissions_for=lambda _: SimpleNamespace(view_channel=True, send_messages=True))
        guild = SimpleNamespace(id=1, me=SimpleNamespace(id=99, top_role=SimpleNamespace(position=10)),
            get_member=members.get, get_role=lambda _: role, get_channel_or_thread=lambda _: channel)
        self.context = SimpleNamespace(guild=guild, member=SimpleNamespace(id=8),
            state=AgentTurnState(), source_message=SimpleNamespace(channel=channel),
            roster_queries=None, role_connection_queries=None, history=())

    async def test_one_role_step_counts_its_targets_once(self):
        arguments = {"role_id": 12, "member_ids": [4, 5]}
        with patch("elbow_helper.features.agent.discord_actions.roles.require_evidence_access",
                   new_callable=AsyncMock):
            await prepare_add_roles(self.context, arguments)
        self.assertEqual(len(self.context.state.proposed_changes), 2)
        proposals = [replace(action, step_id="roles", capability_name="add_discord_roles",
                             checked_arguments=arguments) for action in self.context.state.proposed_changes]
        allowed = [{"capability": "add_discord_roles", "fixed_values": {"role_id": 12},
                    "variable_fields": ["member_ids"], "max_targets": 2,
                    "scope_text": "Add the chosen role to members"}]
        self.assertTrue(within_scope(proposals, allowed))
        self.assertFalse(within_scope(proposals, [{**allowed[0], "max_targets": 1}]))
        widened = [replace(action, checked_arguments={**arguments, "member_ids": [4, 5, 6]})
                   for action in proposals]
        self.assertFalse(within_scope(widened, allowed))

    async def test_post_parts_share_checked_content_and_fixed_ping_options(self):
        arguments = {"channel_id": 20, "text": "Synthetic content. " * 200,
                     "ping_everyone": False, "ping_role_ids": []}
        with patch("elbow_helper.features.agent.discord_actions.messages.require_evidence_access",
                   new_callable=AsyncMock):
            result = await prepare_post(self.context, arguments)
        self.assertEqual(result["status"], "confirmation_required")
        proposals = [replace(action, step_id="post", capability_name="post_discord_message",
                             checked_arguments=arguments) for action in self.context.state.proposed_changes]
        self.assertGreater(len(proposals), 1)
        allowed = [{"capability": "post_discord_message",
                    "fixed_values": {"channel_id": 20, "ping_everyone": False, "ping_role_ids": []},
                    "variable_fields": ["text"], "max_targets": 1, "scope_text": "Post in the chosen channel"}]
        self.assertTrue(within_scope(proposals, allowed))
        widened = [replace(action, checked_arguments={**arguments, "ping_everyone": True})
                   for action in proposals]
        self.assertFalse(within_scope(widened, allowed))
        conflicting = [proposals[0], replace(proposals[1], checked_arguments={**arguments, "text": "Other"})]
        self.assertFalse(within_scope(conflicting, allowed))

    def test_ping_fields_cannot_be_variable(self):
        for field in ("ping_everyone", "ping_role_ids"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_scope([{"capability": "post_discord_message", "fixed_values": {},
                                 "variable_fields": [field], "max_targets": 1,
                                 "scope_text": "Post in the chosen channel"}])
