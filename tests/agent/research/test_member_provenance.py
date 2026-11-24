"""Member-target provenance is request-scoped and independent of phrasing."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.actions.contracts import ActionClass
from elbow_helper.agent.engine.tool_call import execute_tool
from elbow_helper.agent.research.members import find_discord_members
from elbow_helper.agent.research.member_provenance import member_target_issue
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from tests.agent.research.test_member_identity import context


def reply(text, *, self_target=False):
    return AgentStep("", (AgentToolCall("reply", "submit_request_plan", json.dumps({
        "goal": "Reply to the requester", "effort": "low", "output": "text",
        "reply": text, "self_target": self_target, "steps": [],
    })),), AgentUsage())


class MemberProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresolved_action_ids_are_refused_before_handler_runs(self):
        for args, scope in (
            ({"text": "Hello <@42>"}, None),
            ({"member_ids": [101]}, None),
            ({"dm_member_ids": [101]}, None),
            ({"user": 101}, {"entities": [{"kind": "discord_member", "key": 101}]}),
            ({"targets": [{"target_type": "member", "target_id": 101}]}, None),
        ):
            with self.subTest(arguments=args):
                handler = AsyncMock(return_value={"status": "complete"})
                result = json.loads(await execute_tool(
                    name="synthetic_action", handler=handler, context=context(),
                    arguments=args, capability_scope=scope, action_class=ActionClass.OUTPUT,
                ))
                self.assertIn("Resolve them first", result["error"])
                handler.assert_not_awaited()

    async def test_explicit_mentions_allow_actions_for_any_request_phrasing(self):
        for request in ("Would <@101> get a nudge?", "<@!101> needs to see this", "dürfte <@101> helfen?"):
            target = context()
            target.state.request_text = request
            target.source_message.raw_mentions = [101]
            self.assertEqual(member_target_issue(target, {"text": "<@101> hello"}), "")
            self.assertTrue(member_target_issue(target, {"text": "<@42> hello"}))

    async def test_only_the_actual_request_reply_author_is_allowed(self):
        target = context()
        target.source_message.reference = SimpleNamespace(message_id=200, channel_id=100)
        target.state.local_messages = [
            {"message_id": 199, "author_id": 42}, {"message_id": 200, "author_id": 101},
        ]
        self.assertEqual(member_target_issue(target, {"text": "<@101>"}), "")
        self.assertTrue(member_target_issue(target, {"text": "<@42>"}))
        target.source_message.reference.channel_id = 999
        self.assertTrue(member_target_issue(target, {"text": "<@101>"}))
        target.source_message.reference = None
        target.state.reply_to = 200
        self.assertTrue(member_target_issue(target, {"text": "<@101>"}))

    async def test_self_flag_authorizes_only_context_member(self):
        target = context()
        self.assertTrue(member_target_issue(target, {"member_id": 42}))
        target.state.member_self_target = True
        self.assertEqual(member_target_issue(target, {"member_id": 42}), "")
        self.assertEqual(member_target_issue(target, {"dm_member_ids": [42]}), "")
        self.assertTrue(member_target_issue(target, {"dm_member_ids": [42, 101]}))
        self.assertTrue(member_target_issue(target, {"member_id": 101}))
        self.assertTrue(member_target_issue(target, {"member_id": 42}, self_target="true"))

    async def test_resolved_request_reply_is_accepted_only_for_the_same_message_channel_and_guild(self):
        target = context()
        resolved = SimpleNamespace(
            id=200, channel=target.source_message.channel, guild=target.guild,
            author=SimpleNamespace(id=101),
        )
        target.source_message.reference = SimpleNamespace(message_id=200, channel_id=100, resolved=resolved)
        self.assertEqual(member_target_issue(target, {"member_id": 101}), "")
        resolved.guild = SimpleNamespace(id=999)
        self.assertTrue(member_target_issue(target, {"member_id": 101}))

    async def test_only_unique_lookup_grants_provenance_and_retains_name_hint(self):
        target = context()
        ambiguous = await find_discord_members(target, {"query": "Ahmad"})
        self.assertEqual(ambiguous["status"], "needs_input")
        self.assertTrue(member_target_issue(target, {"member_id": 101}))
        resolved = await find_discord_members(target, {"query": "Ahmad (amaj)"})
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(member_target_issue(target, {"member_id": 101}), "")
        self.assertTrue(member_target_issue(target, {"member_id": 42}))
        self.assertEqual(target.state.resolved_members, {101: "Ahmad (amaj)"})


    async def test_failed_lookup_cannot_leave_member_provenance(self):
        target = context()

        async def failing_lookup(ctx, arguments):
            await find_discord_members(ctx, arguments)
            raise RuntimeError("Synthetic lookup failure")

        result = await execute_tool(name="find_discord_members", handler=failing_lookup,
                                    arguments={"query": "Ahmad (amaj)"}, context=target)
        self.assertIn("error", json.loads(result))
        self.assertEqual(target.state.resolved_members, {})
