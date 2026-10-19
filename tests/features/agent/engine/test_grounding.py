"""Generic adversarial personal attributions and final-answer routes."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.engine.grounding import (
    GROUNDING_UNAVAILABLE, GroundingError, ground_answer,
)
from elbow_helper.features.agent.engine.flow import AnswerFlow
from elbow_helper.features.agent.engine.rounds import AgentLimitReached
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.capability_contract import CapabilityContract
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.plan.results import compact_result
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage, AgentToolDefinition
from features.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan, _step


MESSAGE = {"message_id": 101, "author_id": 202, "author": "Member Alpha",
           "content": "Lower-level recruits may need a different placement.",
           "source": "https://discord.com/channels/3/91/101"}


class GroundingTests(unittest.TestCase):
    def test_profile_and_roster_fields_cannot_establish_unrelated_facts(self):
        for kind, data, answer in (
            ("player", {"attackWins": 0}, "Account Alpha is inactive."),
            ("player", {"attackWins": 0}, "Account Alpha has no war participation."),
            ("clan", {"members": 48}, "48 is the hard cap."),
            ("clan", {"memberList": [{"clanRank": 48}]}, "Account Alpha is the newest recruit."),
        ):
            with self.subTest(answer=answer), self.assertRaises(GroundingError):
                ground_answer(answer, {"read": {"source": "clash_api", "kind": kind,
                                               "items": [{"data": data}]}})
        answer = "A zero profile counter does not prove inactivity."
        self.assertEqual(ground_answer(answer, {"read": {"source": "clash_api", "kind": "player"}}), answer)

    def test_unrelated_placement_message_cannot_become_an_objection(self):
        for answer in (
            "Member Alpha objected to Applicant Beta's town hall.",
            "Member Alpha objected to Applicant Beta's town hall. " + MESSAGE["source"],
            "Member Alpha wants Applicant Beta removed. [[message:101]]",
            "According to Member Alpha, Applicant Beta must leave. [[message:101]]",
            "I think Member Alpha wants Applicant Beta removed.",
            "Member Alpha's objection was about Applicant Beta's town hall.",
            "Member Alpha is against keeping Applicant Beta.",
            "A TH15 in war isn't the scandal Member Alpha's making it.",
            "Member Alpha's only crime is recruiting lower-level accounts.",
            "That is the fuss Member Alpha raised about Applicant Beta.",
        ):
            with self.subTest(answer=answer), self.assertRaises(GroundingError):
                ground_answer(answer, {"read": {"messages": [MESSAGE]}})

    def test_sources_are_not_invented_for_a_missing_fact(self):
        for answer in (
            "The actual first name is on the leadership records.",
            "You can find their real name in leadership records.",
            "Leadership records contain the founder's real name.",
        ):
            for evidence in ({}, {"read": {"sections": [{"title": "Family history", "body": "Synthetic lore."}]}},
                             {"read": {"error": "denied", "source_coverage": {"records": {}}}}):
                with self.subTest(answer=answer, evidence=evidence), self.assertRaises(GroundingError):
                    ground_answer(answer, evidence)

    def test_unrelated_fuss_and_record_limit_explanations_are_allowed(self):
        for answer in ("No need to make a fuss. My recommendation: keep Applicant Beta.",
                       "I haven't read leadership records for this answer.",
                       "The available family history doesn't give a real name."):
            self.assertEqual(ground_answer(answer, {}), answer)

    def test_binding_renders_actual_author_and_words_with_source(self):
        answer = ground_answer("[[message:101]]\nMy recommendation: keep Applicant Beta.",
                               {"read": {"messages": [MESSAGE]}})
        self.assertIn("Member Alpha", answer)
        self.assertIn(MESSAGE["source"], answer)
        self.assertIn("> " + MESSAGE["content"], answer)
        self.assertIn("My recommendation: keep Applicant Beta.", answer)

    def test_old_unknown_failed_and_excerpted_sources_do_not_qualify(self):
        for evidence in ({}, {"read": {"error": "denied", "messages": [MESSAGE]}},
                         {"read": {"excerpt": str(MESSAGE)}},
                         {"read": {"messages": [{**MESSAGE, "source": "https://example.invalid/101"}]}}):
            with self.subTest(evidence=evidence), self.assertRaises(GroundingError):
                ground_answer("[[message:101]]", evidence)

    def test_compacted_messages_work_and_conflicting_sources_fail(self):
        rows = [{**MESSAGE, "message_id": index,
                 "source": f"https://discord.com/channels/3/91/{index}"} for index in range(101, 111)]
        self.assertIn(MESSAGE["content"], ground_answer("[[message:101]]", compact_result(rows)))
        with self.assertRaises(GroundingError):
            ground_answer("[[message:101]]", [MESSAGE, {**MESSAGE, "author_id": 999}])

    def test_source_text_is_quoted_and_mentions_escaped(self):
        source = {**MESSAGE, "content": "First line\n@everyone **second line**"}
        answer = ground_answer("[[message:101]]", [source])
        self.assertNotIn("@everyone", answer)
        self.assertIn("\n> First line\n> ", answer)


class GroundingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def answer(self, steps, *, payload=None, context=None):
        session = _Session(steps, [])
        registry = {"read_value": RegisteredAgentTool(
            AgentToolDefinition("read_value", "Read synthetic evidence.",
                                {"type": "object", "properties": {}}),
            AsyncMock(return_value=payload or {}), contract=CapabilityContract(),
        )}
        with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
            answer = await AgentService(_Model(session)).answer(
                question="Review the applicant", local_context="", context=context or _context())
        return answer, session

    async def test_direct_answer_is_regenerated_once(self):
        answer, session = await self.answer([
            AgentStep("Member Alpha wants Applicant Beta removed.", (), AgentUsage()),
            AgentStep("My recommendation: keep Applicant Beta.", (), AgentUsage()),
        ])
        self.assertEqual(answer, "My recommendation: keep Applicant Beta.")
        self.assertEqual(len(session.calls), 2)
        self.assertFalse(session.calls[-1][1])

    async def test_retained_evidence_is_not_a_current_turn_read(self):
        context = _context()
        context.state.grounding_evidence = {"old": {"messages": [MESSAGE]}}
        answer, _ = await self.answer([
            AgentStep("[[message:101]]", (), AgentUsage()),
            AgentStep("[[message:101]]", (), AgentUsage()),
        ], context=context)
        self.assertEqual(answer, GROUNDING_UNAVAILABLE)

    async def test_repeated_unsupported_answer_fails_closed(self):
        bad = AgentStep("Member Alpha objected to Applicant Beta.", (), AgentUsage())
        answer, session = await self.answer([bad, bad])
        self.assertEqual(answer, GROUNDING_UNAVAILABLE)
        self.assertEqual(len(session.calls), 2)

    async def test_indirect_personal_jab_and_invented_record_source_are_repaired(self):
        for unsupported in ("It isn't the scandal Member Alpha's making it.",
                            "Their real name is on the leadership records."):
            with self.subTest(answer=unsupported):
                answer, session = await self.answer([
                    AgentStep(unsupported, (), AgentUsage()),
                    AgentStep("The available evidence doesn't establish that.", (), AgentUsage()),
                ])
                self.assertEqual(answer, "The available evidence doesn't establish that.")
                self.assertEqual(len(session.calls), 2)
                self.assertFalse(session.calls[-1][1])

    async def test_tool_answer_requires_binding_and_preserves_read_evidence(self):
        answer, session = await self.answer([
            _model_step(_plan([_step("read")])),
            AgentStep("Member Alpha objected to Applicant Beta. " + MESSAGE["source"], (), AgentUsage()),
            AgentStep("[[message:101]]\nMy recommendation: keep Applicant Beta.", (), AgentUsage()),
        ], payload={"messages": [MESSAGE]})
        self.assertIn(MESSAGE["content"], answer)
        self.assertNotIn("objected", answer)
        self.assertEqual(len(session.calls), 3)

    async def test_limit_reply_cannot_bypass_guard(self):
        context = _context()
        flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                          rounder=SimpleNamespace(budget_limited=False, advance=AsyncMock(return_value=AgentStep(
                              "Member Alpha wants removal.", (), AgentUsage()))),
                          runner=None, request_id=1)
        flow.completed_results = {"read": {"value": 1}}
        with patch.object(flow, "_run", AsyncMock(side_effect=AgentLimitReached("Limit"))), patch.object(
                flow, "_limit_reply", AsyncMock(return_value="Member Alpha objected.")):
            self.assertEqual(await flow.run(), GROUNDING_UNAVAILABLE)

    async def test_access_loss_during_repair_never_returns_source_text(self):
        from elbow_helper.features.agent.access import AgentAccessLost
        context = _context()
        flow = AnswerFlow(context=context, session=None, registry={}, budget=None,
                          rounder=SimpleNamespace(advance=AsyncMock(return_value=AgentStep(
                              "Synthetic answer", (), AgentUsage()))), runner=None, request_id=1)
        with patch("elbow_helper.features.agent.engine.flow.require_evidence_access",
                   AsyncMock(side_effect=[None, AgentAccessLost("lost")])):
            with self.assertRaises(AgentAccessLost):
                await flow._grounded_reply("Member Alpha objected.")
