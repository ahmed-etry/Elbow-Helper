"""Member-target provenance is request-scoped and independent of phrasing."""

from features.agent.engine.helpers import LegacyCatalogueMixin

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.tool_call import execute_tool
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.engine.capability_contract import CapabilityContract
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.research.members import find_discord_members
from elbow_helper.features.agent.research.member_provenance import member_target_issue
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentToolDefinition, AgentUsage
from tests.features.agent_plan_helpers import plan_call
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession
from features.agent.research.test_member_identity import context


def reply(text, *, self_target=False):
    return AgentStep("", (AgentToolCall("reply", "submit_request_plan", json.dumps({
        "goal": "Reply to the requester", "effort": "low", "output": "text",
        "reply": text, "self_target": self_target, "steps": [],
    })),), AgentUsage())


class MemberProvenanceTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
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

    async def test_non_target_numbers_and_read_actions_are_not_blocked(self):
        target = context()
        self.assertEqual(member_target_issue(target, {"channel_id": 101, "message_id": 42}), "")
        handler = AsyncMock(return_value={"members": []})
        result = await execute_tool(name="read_members", handler=handler, context=target,
                                    arguments={"member_ids": [101]}, action_class=ActionClass.READ)
        self.assertNotIn("error", json.loads(result))
        handler.assert_awaited_once()
        self.assertEqual(target.state.resolved_members, {})

    async def test_action_can_use_only_the_member_resolved_by_an_earlier_step(self):
        for query, called in (("Ahmad (amaj)", True), ("Ahmad", False)):
            with self.subTest(query=query):
                registry = build_agent_tools()
                action = AsyncMock(return_value={"status": "complete"})
                registry["synthetic_action"] = RegisteredAgentTool(
                    AgentToolDefinition("synthetic_action", "Synthetic member action.", {
                        "type": "object", "properties": {"member_id": {"type": "integer"}},
                        "required": ["member_id"], "additionalProperties": False,
                    }), action, action_class=ActionClass.OUTPUT,
                    contract=CapabilityContract(entity_fields=(("member_id", "discord_member"),)),
                )
                plan = plan_call(
                    AgentToolCall("find", "find_discord_members", json.dumps({"query": query})),
                    AgentToolCall("act", "synthetic_action", '{"member_id":101}'),
                    dependencies={"act": ["find"]},
                )
                session = _AgentSession([
                    AgentStep("", (plan,), AgentUsage()), AgentStep("Done." if called else "Which Ahmad?", (), AgentUsage()),
                ])
                with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
                    answer = await AgentService(_AgentModel(session)).answer(
                        question="Give Ahmad amaj a nudge", local_context="", context=context(),
                    )
                self.assertEqual(action.await_count, int(called))
                self.assertEqual(answer, "Done." if called else "Which Ahmad?")

    async def test_failed_lookup_cannot_leave_member_provenance(self):
        target = context()

        async def failing_lookup(ctx, arguments):
            await find_discord_members(ctx, arguments)
            raise RuntimeError("Synthetic lookup failure")

        result = await execute_tool(name="find_discord_members", handler=failing_lookup,
                                    arguments={"query": "Ahmad (amaj)"}, context=target)
        self.assertIn("error", json.loads(result))
        self.assertEqual(target.state.resolved_members, {})

    async def test_lookup_provenance_merges_across_steps_and_revisions(self):
        target = context()
        lookup = plan_call(AgentToolCall("find", "find_discord_members", '{"query":"Ahmad (amaj)"}'))
        session = _AgentSession([
            AgentStep("", (lookup,), AgentUsage()), reply("<@101> synthetic roast"),
        ])
        answer = await AgentService(_AgentModel(session)).answer(
            question="Make Ahmad amaj laugh at himself", local_context="", context=target,
        )
        self.assertEqual(answer, "<@101> synthetic roast")
        self.assertEqual(target.state.resolved_members, {101: "Ahmad (amaj)"})

    async def test_model_asks_after_ambiguous_lookup(self):
        target = context()
        lookup = plan_call(AgentToolCall("find", "find_discord_members", '{"query":"Ahmad"}'))
        session = _AgentSession([
            AgentStep("", (lookup,), AgentUsage()), AgentStep("Which Ahmad: owner or amaj?", (), AgentUsage()),
        ])
        answer = await AgentService(_AgentModel(session)).answer(
            question="Could Ahmad get a nudge?", local_context="", context=target,
        )
        self.assertEqual(answer, "Which Ahmad: owner or amaj?")
        self.assertEqual(target.state.resolved_members, {})

    async def test_model_handles_pronouns_and_unusual_phrasings_without_preflight(self):
        for question in ("tag him", "fais-lui signe", "Perhaps a nudge would help him?"):
            target = context()
            target.source_message.reference = SimpleNamespace(message_id=200, channel_id=100)
            target.state.local_messages = [{"message_id": 200, "author_id": 101}]
            session = _AgentSession([AgentStep("<@101> over here", (), AgentUsage())])
            model = _AgentModel(session)
            answer = await AgentService(model).answer(question=question, local_context="", context=target)
            self.assertEqual(answer, "<@101> over here")
            self.assertIsNotNone(model.request)

    async def test_missing_pronoun_and_unproven_roast_are_left_to_model_clarification(self):
        for question in ("tag him", "Roast ahmad amaj too"):
            session = _AgentSession([
                AgentStep("<@42> synthetic roast", (), AgentUsage()),
                AgentStep("Who did you mean?", (), AgentUsage()),
            ])
            answer = await AgentService(_AgentModel(session)).answer(
                question=question, local_context="Nearby owner: member_id=42", context=context(),
            )
            self.assertEqual(answer, "Who did you mean?")
            self.assertEqual(len(session.calls), 2)
            self.assertIn("Resolve them first", session.instructions[1])

    async def test_self_flag_is_a_validated_plan_argument(self):
        session = _AgentSession([reply("<@42> synthetic self roast", self_target=True)])
        answer = await AgentService(_AgentModel(session)).answer(
            question="Roast me", local_context="", context=context(),
        )
        self.assertEqual(answer, "<@42> synthetic self roast")

    async def test_invalid_self_flag_does_not_authorize_the_asker(self):
        invalid = reply("<@42> synthetic self roast", self_target="true")
        session = _AgentSession([invalid, AgentStep("Who did you mean?", (), AgentUsage())])
        target = context()
        answer = await AgentService(_AgentModel(session)).answer(
            question="Roast me", local_context="", context=target,
        )
        self.assertEqual(answer, "Who did you mean?")
        self.assertFalse(target.state.member_self_target)

    async def test_late_unproven_mention_is_corrected_by_the_model_or_fails_without_a_ping(self):
        for correction, expected in (("Which Ahmad did you mean?", "Which Ahmad did you mean?"),
                                     ("<@42> still wrong", "Which member do you mean? Please mention them.")):
            lookup = plan_call(AgentToolCall("find", "find_discord_members", '{"query":"Ahmad (amaj)"}'))
            session = _AgentSession([
                AgentStep("", (lookup,), AgentUsage()),
                AgentStep("<@42> wrong roast target", (), AgentUsage()),
                AgentStep(correction, (), AgentUsage()),
            ])
            answer = await AgentService(_AgentModel(session)).answer(
                question="Make Ahmad amaj laugh", local_context="", context=context(),
            )
            self.assertEqual(answer, expected)
            self.assertIn("Resolve them first", session.instructions[-1])

    async def test_previous_request_lookup_does_not_authorize_a_new_reply(self):
        target = context()
        target.state.resolved_members = {101: "Ahmad amaj"}
        target.state.member_self_target = True
        session = _AgentSession([
            AgentStep("<@101> synthetic roast", (), AgentUsage()),
            AgentStep("Which member?", (), AgentUsage()),
        ])
        answer = await AgentService(_AgentModel(session)).answer(
            question="Tell him", local_context="", context=target,
        )
        self.assertEqual(answer, "Which member?")
        self.assertEqual(target.state.resolved_members, {})
        self.assertFalse(target.state.member_self_target)
