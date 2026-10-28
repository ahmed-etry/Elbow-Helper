"""Anonymous reproductions of gaps found by independent repair verification."""

from dataclasses import replace
from agent.engine.helpers import LegacyCatalogueMixin

import json
from types import SimpleNamespace
from elbow_helper.agent.wording import AGENT_ANSWER_UNFINISHED
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.access import AgentAccessLost
from elbow_helper.agent.conversation.context import compile_context
from elbow_helper.agent.conversation.state import ConversationRecord, ConversationTurn
from elbow_helper.agent.delivery import AgentDeliveryMixin
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from agent.engine import test_repair_conversations as repairs
from agent.engine.test_agent_plan_flow import _Model, _Session


class VerificationFixTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    context = repairs.RepairConversationTests.context

    def test_named_and_pronoun_followups_keep_all_authorized_cases_and_their_access(self):
        correction = "I asked about competitive promotion, not CWL. Use regular wars and same TH or +1 triples."

        def turn(identity, name, question, answer, *, restricted=False):
            evidence = () if name is None else (json.dumps({
                "tool": "read_clash", "result_complete": True, "result": json.dumps({
                    "source": "clash_api", "kind": "player", "items": [{"tag": "#P0" if name == "Example" else "#Q0",
                        "status": "ok", "data": {"name": name}}],
                }),
            }),)
            return ConversationTurn("{}", frozenset({92 if restricted else 91}),
                required_access=frozenset({"lead"}) if restricted else frozenset(), record=ConversationRecord(
                    identity, 41, "2026-10-01T00:00:00+00:00", question, answer, answer, "", evidence, (), (), True))

        relevant = (turn(1, "Example", "Is Example ready for promotion?", "Evaluate CWL readiness."),
                    turn(2, None, correction, "Understood."))
        for question in ("Does he meet that bar?", "Does Example meet that bar?"):
            context = self.context(lambda *_a, **_kw: None)
            context.state.authorized_history = relevant
            compiled = compile_context(question=question, local_context="", context=context, tools=())
            self.assertEqual(compiled.selected_request_ids, (1, 2))
            self.assertIn(correction, compiled.prompt)

        unrelated = (turn(3, "Another", "Is Another ready for a different review?", "private_case_marker", restricted=True),
                     turn(4, None, "I meant that other review's criteria.", "private_correction_marker", restricted=True))
        context = self.context(lambda *_a, **_kw: None)
        context.state.authorized_history = (*relevant, *unrelated)
        compiled = compile_context(question="Does Example meet that bar?", local_context="", context=context, tools=())
        self.assertEqual(compiled.selected_request_ids, (1, 2, 3, 4))
        self.assertIn(correction, compiled.prompt)
        self.assertIn("private_case_marker", compiled.prompt)
        self.assertIn("private_correction_marker", compiled.prompt)
        self.assertEqual(context.state.source_channels, {91, 92})
        self.assertEqual(context.state.required_access, {"lead"})

    async def test_identical_semantic_member_errors_stop_before_a_third_plan(self):
        context = self.context(lambda *_a, **_kw: None)
        session = _Session([
            *(repairs.planned("members", "read_discord_members", {}) for _ in range(3)),
            AgentStep("No member selection was established.", (), AgentUsage(1000, 100)),
        ], [])
        await AgentService(_Model(session)).answer(
            question="Review the members in this conversation.", local_context="", context=context)
        self.assertEqual(len(session.calls), 3)
        self.assertFalse(session.calls[-1][1])
        error = json.loads(session.calls[1][0][0].content)["results"]["members"]
        self.assertEqual(error["cause"], "invalid_arguments")
        self.assertEqual(set(error["invalid_fields"]), {"member_ids", "role_ids", "sort"})
        self.assertIn("member_ids", error["argument_schema"]["properties"])

    async def test_additional_key_private_signal_without_withheld_evidence_never_reaches_delivery(self):
        context = self.context(lambda *_a, **_kw: None)
        signal = json.dumps({"needs_restricted_knowledge": True, "section_ids": ["section"], "reason": "Need the policy"})
        session = _Session([AgentStep(signal, (), AgentUsage(1000, 100))], [])
        answer = await AgentService(_Model(session)).answer(
            question="Explain the relevant guidance.", local_context="", context=context)
        delivery = AgentDeliveryMixin()
        delivery.bot = context.bot
        await delivery.send_response(context.source_message, answer, None, context=context)
        self.assertEqual(context.source_message.reply.await_args.args[0],
                         AGENT_ANSWER_UNFINISHED)
        self.assertNotIn("needs_restricted_knowledge", context.source_message.reply.await_args.args[0])
        self.assertFalse(context.state.scope_audit.offered)
        self.assertEqual(len(session.calls), 1)

    async def test_semantic_errors_allow_a_valid_correction_and_retain_independent_results(self):
        cases = (
            ("read_discord_members", {}, {"member_ids": [41]}),
            ("read_clash", {"kind": "player", "tags": ["invalid"]}, {"kind": "player", "tags": ["#P0"]}),
            ("read_ranked_stats", {"account_tags": ["#P0"], "after": "2026-01-01", "before": "2026-10-04"},
             {"account_tags": ["#P0"], "after": "2026-10-01", "before": "2026-10-04"}),
        )
        for capability, invalid, corrected in cases:
            for repeat in (False, True):
                with self.subTest(capability=capability, repeat=repeat):
                    context = self.context(lambda *_a, **_kw: SimpleNamespace(ok=True, status=200,
                        payload={"name": "Example", "leagueTier": {"name": "Dragon League 30"}}))
                    context.member.name = "Example"
                    window = Mock(return_value={"complete": True, "accounts": [{
                        "player_tag": "#P0", "battle_records": 2, "counts_complete": True,
                        "first_observed_at": "2026-10-01", "last_observed_at": "2026-10-04",
                    }]})
                    context = replace(context, ranked_queries=SimpleNamespace(window=window))
                    first = repairs._plan([
                        {"id": "kept", "capability": "read_clash", "arguments": {"kind": "player", "tags": ["#P0"]}, "depends_on": []},
                        {"id": "fix", "capability": capability, "arguments": invalid, "depends_on": []},
                    ])
                    session = _Session([
                        replace(repairs._model_step(first), usage=AgentUsage(1000, 100)),
                        repairs.planned("fix", capability, invalid if repeat else corrected),
                        AgentStep("The completed evidence is available.", (), AgentUsage(1000, 100)),
                    ], [])
                    await AgentService(_Model(session)).answer(question="Analyze these member lookups.", local_context="", context=context)
                    feedback = json.loads(session.calls[1][0][0].content)["results"]
                    self.assertEqual(feedback["fix"]["cause"], "invalid_arguments")
                    self.assertTrue(feedback["fix"]["invalid_fields"])
                    self.assertTrue(feedback["fix"]["argument_schema"]["properties"])
                    self.assertEqual(feedback["kept"]["items"][0]["data"]["leagueTier"]["name"], "Dragon League 30")
                    self.assertEqual(len(session.calls), 3)
                    self.assertEqual(window.call_count, int(capability == "read_ranked_stats" and not repeat))
                    if repeat:
                        self.assertFalse(session.calls[-1][1])
                        self.assertIn("Dragon League 30", str(session.calls[-1][0]))
                    else:
                        result = json.loads(session.calls[-1][0][0].content)["results"]["fix"]
                        self.assertNotIn("error", result)

    async def test_transient_member_lookup_failure_is_retried_with_real_io(self):
        context = self.context(lambda *_a, **_kw: None)
        context.member.name = "Example"
        arguments = {"role_ids": [context.member.roles[0].id]}
        session = _Session([
            repairs.planned("members", "read_discord_members", arguments),
            repairs.planned("members", "read_discord_members", arguments),
            AgentStep("Member details are available.", (), AgentUsage(1000, 100)),
        ], [])
        complete = AsyncMock(side_effect=[TimeoutError(), (context.member,)])
        with patch("elbow_helper.agent.research.members._complete_members", complete):
            await AgentService(_Model(session)).answer(
                question="Review the members in this conversation.", local_context="", context=context)
        self.assertEqual(complete.await_count, 2)
        self.assertEqual(len(session.calls), 3)
        result = json.loads(session.calls[-1][0][0].content)["results"]["members"]
        self.assertEqual(result["members"][0]["member_id"], context.member.id)

    async def test_valid_member_correction_rechecks_permission(self):
        context = self.context(lambda *_a, **_kw: None)
        session = _Session([repairs.planned("members", "read_discord_members", {}),
                            repairs.planned("members", "read_discord_members", {"member_ids": [41]})], [])
        advance = session.advance

        async def revoke(*args, **kwargs):
            step = await advance(*args, **kwargs)
            if len(session.calls) == 2:
                context.member.roles.clear()
            return step

        session.advance = revoke
        with self.assertRaises(AgentAccessLost):
            await AgentService(_Model(session)).answer(
                question="Review the members in this conversation.", local_context="", context=context)
        self.assertEqual(len(session.calls), 2)
        self.assertFalse(any('"members": [' in record for record in context.state.evidence))

    async def test_grounding_rewrite_cannot_release_private_protocol(self):
        context = self.context(lambda *_a, **_kw: None)
        session = _Session([
            AgentStep("[[message:999]]", (), AgentUsage(1000, 100)),
            AgentStep('{"needs_restricted_knowledge":true,"reason":"internal_reason_marker"}', (), AgentUsage(1000, 100)),
        ], [])
        answer = await AgentService(_Model(session)).answer(
            question="Explain the relevant guidance.", local_context="", context=context)
        delivery = AgentDeliveryMixin()
        delivery.bot = context.bot
        await delivery.send_response(context.source_message, answer, None, context=context)
        self.assertEqual(context.source_message.reply.await_args.args[0],
                         AGENT_ANSWER_UNFINISHED)
        self.assertNotIn("internal_reason_marker", answer)
        self.assertEqual(len(session.calls), 2)
