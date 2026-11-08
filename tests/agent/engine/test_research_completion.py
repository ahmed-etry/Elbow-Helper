"""Synthetic multi-read tasks finish within shared limits or retain findings."""

import asyncio
from dataclasses import replace
from elbow_helper.agent.wording import AGENT_ANSWER_UNFINISHED
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.engine.budgets import ContextBudget, RequestLimits
from elbow_helper.agent.wording import FAILURE_MESSAGE, AGENT_RESEARCH_UNFINISHED
from elbow_helper.infrastructure.ai import AgentUsage
from agent.engine import test_request_cost as cost

plan, response, Session = cost.plan, cost.response, cost.Session


class ResearchCompletionTests(unittest.IsolatedAsyncioTestCase):
    flow = cost.RequestCostTests.flow


    async def test_five_parallel_checks_follow_cohort_read_in_one_round(self):
        from elbow_helper.agent.models import RegisteredAgentTool
        from elbow_helper.infrastructure.ai import AgentToolDefinition
        from agent.engine.test_agent_plan_flow import _model_step, _plan

        active, peak = 0, 0
        async def check(_, arguments):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return {"member_name": f"Member {arguments['member_id']}", "missed": 1}
        registry = {"read_clash": RegisteredAgentTool(AgentToolDefinition(
            "read_clash", "Read synthetic cohort.", {"type": "object", "properties": {}}),
            AsyncMock(return_value={"members": [{"member_id": n} for n in range(1, 6)]})),
            "read_history": RegisteredAgentTool(AgentToolDefinition(
                "read_history", "Check synthetic explanations.", {"type": "object", "properties": {
                    "member_id": {"type": "integer"}}, "required": ["member_id"]}), check)}
        checks = replace(_model_step(_plan([
            {"id": f"history_{n}", "capability": "read_history", "arguments": {
                "member_id": {"step": "read", "path": ["members", n - 1, "member_id"]}},
             "depends_on": []} for n in range(1, 6)])), usage=AgentUsage(31_000, 700))
        final = "Five members missed one attack each; no explanation was found for these five."
        flow, session, _, _ = self.flow([
            replace(plan(), usage=AgentUsage(29_000, 700)), checks,
            replace(response(final), usage=AgentUsage(35_000, 700)),
        ], question="Which five missed in September, and did they explain?", registry=registry)
        self.assertEqual(await flow.run(), final)
        self.assertGreater(peak, 1)
        self.assertEqual(flow.runner.tool_calls, 6)
        self.assertEqual(len(session.calls), 3)


    async def test_compact_final_uses_remaining_budget_without_catalogue_or_pending_calls(self):
        flow, session, _, _ = self.flow([replace(plan(), usage=AgentUsage(60_000, 100))])
        flow.budget.request_limits = RequestLimits(4, 80_000, 12_000, 4096)
        final = Session([response("Synthetic league confirmed; September war comparison remains unchecked.")], [])
        factory = flow.final_session_factory = Mock(return_value=final)
        with patch.object(ContextBudget, "can_continue_tools", return_value=True):
            answer = await flow.run()
        self.assertIn("September war comparison remains unchecked", answer)
        self.assertEqual(factory.call_args.kwargs["tools"], ())
        self.assertIn("Synthetic league", factory.call_args.kwargs["prompt"])
        self.assertNotIn("submit_request_plan", factory.call_args.kwargs["system_prompt"])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(final.calls), 1)
        self.assertFalse(final.calls[0][1])
        self.assertEqual(flow.budget.spent_rounds, 2)
        self.assertEqual(flow.budget.spent_input, 61_000)

    async def test_hard_exhaustion_does_not_dump_findings_or_make_another_provider_call(self):
        flow, session, _, _ = self.flow([plan(), plan(identity="extra")])
        flow.budget.request_limits = RequestLimits(2, 80_000, 12_000, 4096)
        answer = await flow.run()
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
        self.assertNotIn("What league is this account in?", answer)
        self.assertNotIn(FAILURE_MESSAGE, answer)
        self.assertNotIn(AGENT_RESEARCH_UNFINISHED, answer)
        self.assertEqual(len(session.calls), 2)
