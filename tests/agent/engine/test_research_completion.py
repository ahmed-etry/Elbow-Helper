"""Synthetic multi-read tasks finish within shared limits or retain findings."""

import asyncio
from dataclasses import replace
from elbow_helper.agent.wording import AGENT_ANSWER_UNFINISHED
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.engine.budgets import ContextBudget, RequestLimits
from elbow_helper.agent.engine.unfinished import unfinished_answer
from elbow_helper.agent.wording import FAILURE_MESSAGE, AGENT_RESEARCH_UNFINISHED
from elbow_helper.infrastructure.ai import AgentUsage
from agent.engine import test_request_cost as cost

plan, response, Session = cost.plan, cost.response, cost.Session


class ResearchCompletionTests(unittest.IsolatedAsyncioTestCase):
    flow = cost.RequestCostTests.flow

    def test_archived_shape_replay_exposes_prefix_starvation(self):
        from agent.engine.reasoning_replay import PREVIOUS_LIMITS, replay
        from elbow_helper.agent.engine.budgets import RESEARCH_LIMITS
        record = {"rounds": [
            {"phase": "routing", "usage": {"prompt_tokens": 1500, "completion_tokens": 350}},
            *[{"round": n, "usage": {"prompt_tokens": prompt, "completion_tokens": 800}}
              for n, prompt in enumerate((26_500, 34_500, 39_500), 1)],
        ]}
        self.assertEqual(replay(record, PREVIOUS_LIMITS)["first_answer_only_round"], 3)
        revised = replay(record, RESEARCH_LIMITS)
        self.assertIsNone(revised["first_answer_only_round"])
        self.assertTrue(revised["can_research_again"])

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

    async def test_six_dependent_batches_fit_a_realistic_prefix(self):
        steps = [replace(plan(identity=f"read_{n}"), usage=AgentUsage(26_000 + n * 2000, 700))
                 for n in range(6)]
        # Distinct arguments avoid the engine's intentional read deduplication.
        from agent.engine.test_agent_plan_flow import _model_step, _plan
        from elbow_helper.agent.models import RegisteredAgentTool
        from elbow_helper.infrastructure.ai import AgentToolDefinition
        registry = {"read_clash": RegisteredAgentTool(AgentToolDefinition(
            "read_clash", "Read synthetic page.", {"type": "object", "properties": {
                "page": {"type": "integer"}}}), AsyncMock(return_value={"stars": 3}))}
        steps = [replace(_model_step(_plan([{"id": f"page_{n}", "capability": "read_clash",
                 "arguments": {"page": n}, "depends_on": []}])), usage=step.usage)
                 for n, step in enumerate(steps)]
        flow, session, _, _ = self.flow([*steps, response("All six pages checked.")], registry=registry)
        self.assertEqual(await flow.run(), "All six pages checked.")
        self.assertEqual(flow.runner.tool_calls, 6)
        self.assertEqual(len(session.calls), 7)
        self.assertLessEqual(flow.budget.spent_input, flow.budget.request_limits.input_tokens)

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

    def test_fallback_omits_raw_fields_request_and_diagnostics(self):
        answer = unfinished_answer("Who attacked after hibernating?", {
            "good": {"rows": [{"player_name": "Synthetic", "attacks_used": 2,
                               "member_id": 123456789012345678}]},
            "failed": {"error": "secret_transport_failure", "name": "Excluded"},
        })
        self.assertNotIn("Synthetic", answer)
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
        self.assertNotIn("Who attacked after hibernating?", answer)
        self.assertNotIn("123456789012345678", answer)
        self.assertNotIn("secret_transport_failure", answer)
        self.assertNotIn("Excluded", answer)

    def test_hard_cap_does_not_substitute_source_dump_for_synthesis(self):
        answer = unfinished_answer("Compare the entry bar with the explanation", {
            "rules": {"sections": [{"section_id": "synthetic", "title": "Entry bar", "body": "TH14+ required."}]},
            "history": {"messages": [{"message_id": 8, "author_id": 4, "author": "Synthetic member",
                        "content": "I missed an attack.", "source": "https://discord.com/channels/1/2/8"}]},
        })
        self.assertNotIn("TH14+ required", answer)
        self.assertNotIn("I missed an attack", answer)
        self.assertNotIn("Available findings:", answer)
        self.assertIn(AGENT_ANSWER_UNFINISHED, answer)
