"""Member effort limits at initial planning, revision and execution boundaries."""

import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import (
    AGENT_TESTER_ROLE_ID, CORE, LEAD_PLUS, MEMBER_ROLE_ID,
    SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID,
)
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.agent.plan.checker import check_plan
from elbow_helper.infrastructure.ai import AgentStep, AgentToolDefinition, AgentUsage
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from agent.engine.test_agent_plan_flow import (
    _context, _Model, _Session, _model_step, _plan, _step,
)


class MemberEffortTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.read = AsyncMock(return_value={"value": 7})
        self.discussion = AsyncMock(return_value={
            "family": "read_conversation_evidence", "rows": [],
        })
        self.registry = {
            "read_value": RegisteredAgentTool(AgentToolDefinition(
                "read_value", "Read a synthetic value.",
                {"type": "object", "properties": {}},
            ), self.read, contract=CapabilityContract(())),
            "read_conversation_evidence": RegisteredAgentTool(AgentToolDefinition(
                "read_conversation_evidence", "Read a synthetic discussion.",
                {"type": "object", "properties": {
                    "mode": {"type": "string"}, "channel_id": {"type": "integer"},
                    "limit": {"type": "integer"},
                }},
            ), self.discussion, contract=CapabilityContract(())),
        }

    def start(self, plans, role=MEMBER_ROLE_ID, *, assessment_answer=False):
        context = _context()
        context.member.roles = [SimpleNamespace(id=identity)
                                for identity in (AGENT_TESTER_ROLE_ID, role)]
        context.guild.roles = [context.guild.default_role, *context.member.roles]
        content = json.dumps({
            "intent": "Established objective", "intent_source": "request",
            "discussion_message_ids": [], "settled_choices": [], "open_choices": [],
            "facts": "Synthetic answer.", "question": "", "recommendation": "",
        }) if assessment_answer else "Synthetic answer."
        session = _Session([
            *(_model_step(plan) for plan in plans), AgentStep(content, (), AgentUsage()),
        ], [])
        session.steps = [replace(step, usage=AgentUsage(4000, 100)) for step in session.steps]
        model = _Model(session)
        with patch("elbow_helper.agent.engine.service.build_agent_tools",
                   return_value=dict(self.registry)):
            flow, _ = AgentService(model)._start_flow(
                question="Use the supplied values", local_context="", context=context,
                conversation_history="",
            )
        return flow, session, model

    async def test_member_assessment_is_high_without_repair_or_discussion_reads(self):
        for channels in (None, [91]):
            with self.subTest(channels=channels):
                plan = {**_plan([_step("first")], effort="max"), "assessment": True}
                if channels is not None:
                    plan["discussion_channels"] = channels
                else:
                    self.assertFalse(check_plan(plan, self.registry).ok)
                flow, session, _ = self.start([plan])
                with self.assertLogs("elbow_helper.agent.engine.flow", "INFO") as logs:
                    self.assertEqual(await flow.run(), "Synthetic answer.")
                self.assertNotIn("assessment", flow.plan)
                self.assertEqual(flow.plan["effort"], "high")
                self.assertEqual(flow._planning_effort(), AgentReasoningEffort.HIGH)
                self.assertFalse(flow.assessment_requested)
                self.assertEqual(len(session.calls), 2)
                self.assertEqual(session.events, ["model", "model"])
                self.assertIn("first", flow.runner.completed_steps)
                self.discussion.assert_not_awaited()
                limited = [record for record in logs.records if "plan limited" in record.message]
                self.assertEqual(len(limited), 1)
                self.assertEqual(limited[0].levelname, "INFO")
                self.assertIn("request=71", limited[0].message)

    async def test_member_revisions_and_corrections_are_limited_once_per_request(self):
        for correction in (False, True):
            with self.subTest(correction=correction):
                initial = {**_plan([_step("first")], effort="max"), "assessment": True}
                revision = {**_plan([_step("second")], effort="max"), "assessment": True}
                plans = ([{**initial, "output": "invalid"}, initial,
                          {**revision, "output": "invalid"}, revision] if correction
                         else [initial, revision])
                flow, session, _ = self.start(plans)
                with self.assertLogs("elbow_helper.agent.engine.flow", "INFO") as logs:
                    self.assertEqual(await flow.run(), "Synthetic answer.")
                self.assertEqual(len(session.calls), 5 if correction else 3)
                self.assertEqual(set(flow.runner.completed_steps), {"first", "second"})
                self.assertNotIn("assessment", flow.plan)
                self.assertEqual(flow.plan["effort"], "high")
                self.assertFalse(flow.assessment_requested)
                self.discussion.assert_not_awaited()
                self.assertEqual(sum("plan limited" in record.message for record in logs.records), 1)
                if correction:
                    self.assertEqual(session.calls[3][2], AgentReasoningEffort.HIGH)

    async def test_lead_plus_core_and_sleeping_leaders_keep_assessment_and_max(self):
        roles = LEAD_PLUS | CORE | {SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID}
        for role in sorted(roles):
            for revision in (False, True):
                with self.subTest(role=role, revision=revision):
                    self.discussion.reset_mock()
                    assessment = {**_plan([_step("assessed")], effort="max"),
                                  "assessment": True, "discussion_channels": [91],
                                  "finish_after_reads": True}
                    plans = ([_plan([_step("first")]), assessment] if revision else [assessment])
                    flow, session, _ = self.start(plans, role, assessment_answer=True)
                    with self.assertLogs("elbow_helper.agent", "INFO") as logs:
                        self.assertEqual(await flow.run(), "Synthetic answer.")
                    self.assertTrue(flow.assessment_requested)
                    self.assertTrue(flow.plan["assessment"])
                    self.assertEqual(flow.plan["effort"], "max")
                    self.assertEqual(flow._planning_effort(), AgentReasoningEffort.MAX)
                    self.assertEqual(session.calls[-1][2], AgentReasoningEffort.MAX)
                    self.assertEqual(len(session.calls), 3 if revision else 2)
                    self.discussion.assert_awaited_once()
                    self.assertFalse(any("plan limited" in record.message for record in logs.records))

    async def test_execution_guards_cap_member_effort_if_plan_hook_is_bypassed(self):
        plan = {**_plan([_step("first")], effort="max"), "assessment": True,
                "discussion_channels": [91]}
        flow, _, _ = self.start([plan])
        flow.rounder.request_effort = AgentReasoningEffort.MAX
        with patch.object(flow, "_limit_plan", side_effect=lambda value: value):
            self.assertEqual(await flow.run(), "Synthetic answer.")
        self.assertTrue(flow.plan["assessment"])
        self.assertFalse(flow.assessment_requested)
        self.assertEqual(flow._planning_effort(), AgentReasoningEffort.HIGH)
        self.discussion.assert_not_awaited()

    async def test_members_keep_low_and_high_with_shared_system_and_schema(self):
        for effort in ("low", "high"):
            with self.subTest(effort=effort):
                plan = _plan([_step("first")], effort=effort)
                member, session, member_model = self.start([plan])
                leader, _, leader_model = self.start([plan], next(iter(CORE)))
                self.assertEqual(member_model.request["system_prompt"],
                                 leader_model.request["system_prompt"])
                self.assertEqual(member_model.request["tools"], leader_model.request["tools"])
                self.assertEqual(await member.run(), "Synthetic answer.")
                self.assertEqual(member.plan["effort"], effort)
                self.assertFalse(member.plan_limit_logged)
                self.assertEqual(len(session.calls), 2)
                self.assertEqual(await leader.run(), "Synthetic answer.")
