"""Request cost limits cover planning, expansion, recovery and confirmation."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentToolDefinition, AgentUsage
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.features.agent.actions.contracts import ActionClass, ChangePreview, PreparedAction
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.engine.budgets import ContextBudget, RequestLimits, LOOKUP_LIMITS, RESEARCH_LIMITS
from elbow_helper.features.agent.engine.rounds import AgentLimitReached
from elbow_helper.features.agent.engine.routing import EXPAND_CONTEXT, simple_lookup
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import AgentCapabilityEffect, RegisteredAgentTool
from elbow_helper.features.agent.wording import AGENT_RESEARCH_UNFINISHED
from features.agent.engine.test_agent_plan_flow import _context, _Session, _Model, _model_step, _plan, _step


class Session(_Session):
    def replace_tools(self, tools):
        self.tools = tools


def response(text="Checked.", calls=(), **kwargs):
    return AgentStep(text, calls, AgentUsage(1000, 100), **kwargs)


def plan(name="read_clash", identity="read"):
    return replace(_model_step(_plan([
        {**_step(identity), "capability": name},
    ])), usage=AgentUsage(1000, 100))


class RequestCostTests(unittest.IsolatedAsyncioTestCase):
    def flow(self, steps, *, question="What league is this account in?", registry=None,
             context=None, guide=None):
        session = Session(steps, [])
        model = _Model(session)
        tool = RegisteredAgentTool(AgentToolDefinition(
            "read_clash", "Read a fact.", {"type": "object", "properties": {}},
        ), AsyncMock(return_value={"league": "Synthetic league"}))
        with patch("elbow_helper.features.agent.engine.service.build_agent_tools",
                   return_value=registry or {"read_clash": tool}):
            flow, usage = AgentService(model, data_guide=guide)._start_flow(
                question=question, local_context="", context=context or _context(),
                conversation_history="",
            )
        return flow, session, model, usage

    def test_conservative_routing(self):
        for question in ("How many members?", "BE member count", "Extract tags from this message",
                         "No, names only", "What is this account's league?"):
            self.assertTrue(simple_lookup(question), question)
        for question in ("Compare war and ranked activity", "Should we remove this player?",
                         "Count changes across sources", "Why was that count wrong?",
                         "Count members and give them a role", "Count only; run the command",
                         "Export a spreadsheet with the count", "an unspecified task"):
            self.assertFalse(simple_lookup(question), question)

    async def test_lookup_omits_broad_context_and_starts_low(self):
        knowledge = Mock()
        guide = Mock()
        context = replace(_context(), knowledge_store=knowledge)
        flow, session, model, _ = self.flow([plan(), response()], context=context, guide=guide)
        self.assertEqual(await flow.run(), "Checked.")
        knowledge.load.assert_not_called()
        guide.for_levels.assert_not_called()
        self.assertNotIn("<data_guide>", model.request["system_prompt"])
        self.assertEqual(session.calls[0][2], AgentReasoningEffort.LOW)
        self.assertTrue(all(limit <= 4096 for limit in session.output_limits))
        self.assertEqual(flow.budget.spent_rounds, 2)

    async def test_expansion_loads_full_context_once_without_resetting_spending(self):
        guide = Mock()
        guide.for_levels.return_value = "Synthetic data schema"
        knowledge = Mock()
        knowledge.load.return_value.prompt.return_value = SimpleNamespace(
            text="Synthetic community knowledge", required_access=set(),
        )
        context = replace(_context(), knowledge_store=knowledge)
        tool = RegisteredAgentTool(AgentToolDefinition(
            "read_value", "Read a value.", {"type": "object", "properties": {}},
        ), AsyncMock(return_value={"value": 7}))
        expand = response("", (AgentToolCall("expand", EXPAND_CONTEXT, "{}"),))
        flow, session, model, _ = self.flow(
            [expand, plan("read_value"), response()], registry={"read_value": tool},
            context=context, guide=guide,
        )
        self.assertNotIn("read_value:", model.request["system_prompt"])
        self.assertEqual(await flow.run(), "Checked.")
        self.assertEqual(flow.budget.request_limits, RESEARCH_LIMITS)
        self.assertEqual((flow.budget.spent_rounds, flow.budget.spent_input,
                          flow.budget.spent_output), (3, 3000, 300))
        self.assertIn("Synthetic data schema", session.calls[1][0][0].content)
        self.assertIn("Synthetic community knowledge", session.calls[1][0][0].content)
        self.assertIn("read_value:", session.calls[1][0][0].content)
        self.assertEqual(session.calls[1][2], AgentReasoningEffort.HIGH)
        self.assertEqual([tool.name for tool in session.tools], ["submit_request_plan"])
        knowledge.load.assert_called_once()
        guide.for_levels.assert_called_once()

    async def test_research_starts_with_full_context_and_high_effort(self):
        guide = Mock()
        guide.for_levels.return_value = "Synthetic data schema"
        flow, session, model, _ = self.flow(
            [response()], question="Compare multiple sources", guide=guide,
        )
        await flow.run()
        self.assertIn("Synthetic data schema", model.request["system_prompt"])
        self.assertEqual(flow.budget.request_limits, RESEARCH_LIMITS)
        self.assertEqual(session.calls[0][2], AgentReasoningEffort.HIGH)

    async def test_expanded_knowledge_access_is_rechecked_before_generation(self):
        knowledge = Mock()
        knowledge.load.return_value.prompt.return_value = SimpleNamespace(
            text="Restricted evidence", required_access={"lead"},
        )
        context = replace(_context(), knowledge_store=knowledge)
        expand = response("", (AgentToolCall("expand", EXPAND_CONTEXT, "{}"),))
        flow, session, _, _ = self.flow([expand], context=context)
        with self.assertRaises(AgentAccessLost):
            await flow.run()
        self.assertEqual(context.state.required_access, {"lead"})
        self.assertEqual(len(session.calls), 1)

    async def test_plan_corrections_share_the_round_budget(self):
        invalid = plan("not_in_catalogue")
        flow, session, _, _ = self.flow([invalid, plan()])
        flow.budget.request_limits = RequestLimits(2, 80_000, 12_000, 4096)
        with self.assertRaises(AgentLimitReached):
            await flow.run()
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(flow.runner.tool_calls, 0)
        self.assertEqual(flow.budget.spent_rounds, 2)

    async def test_output_continuations_share_the_round_budget(self):
        flow, session, _, _ = self.flow([
            response("Part", output_limit_reached=True), response("More", output_limit_reached=True),
        ])
        flow.budget.request_limits = RequestLimits(2, 80_000, 12_000, 4096)
        with self.assertRaises(AgentLimitReached):
            await flow.run()
        self.assertEqual(flow.budget.spent_rounds, 2)
        self.assertEqual(len(session.calls), 2)

    async def test_input_limit_after_evidence_returns_notice_without_more_spending(self):
        flow, session, _, _ = self.flow([
            replace(plan(), usage=AgentUsage(79_500, 100)),
        ])
        # Read admission is isolated here to exercise a provider usage correction
        # that leaves no room to send the already completed evidence back.
        with patch.object(ContextBudget, "can_continue_tools", return_value=True):
            answer = await flow.run()
        self.assertEqual(answer, AGENT_RESEARCH_UNFINISHED)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(flow.budget.spent_input, 79_500)

    async def test_final_output_is_capped_by_total_remaining_allowance(self):
        flow, session, _, _ = self.flow([response()])
        flow.budget.spent_output = LOOKUP_LIMITS.output_tokens - 1500
        await flow.rounder.advance(allow_tools=False, final=True)
        self.assertEqual(session.output_limits, [1500])

    async def test_confirmation_still_required_after_lookup_expands(self):
        execute = AsyncMock()
        async def prepare(context, arguments):
            context.state.proposed_changes.append(PreparedAction(
                "change", {}, ChangePreview(("Synthetic change",), AsyncMock(return_value=True)), execute,
            ))
            return {"status": "confirmation_required"}
        tool = RegisteredAgentTool(AgentToolDefinition(
            "change", "Prepare a change.", {"type": "object", "properties": {}},
        ), prepare, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE)
        expand = response("", (AgentToolCall("expand", EXPAND_CONTEXT, "{}"),))
        context = replace(_context(), bot=SimpleNamespace(tree=object()))
        with patch("elbow_helper.features.agent.engine.service.build_command_tools", return_value=({}, {})):
            flow, session, _, _ = self.flow(
                [expand, plan("change")], registry={"change": tool}, context=context,
            )
        self.assertIn("Synthetic change", await flow.run())
        self.assertEqual(len(context.state.proposed_changes), 1)
        execute.assert_not_awaited()
        self.assertEqual(len(session.calls), 2)

    async def test_round_exhaustion_returns_partial_without_an_extra_final_call(self):
        flow, session, _, _ = self.flow([plan(), response("Checked league only.")])
        flow.budget.request_limits = RequestLimits(2, 80_000, 12_000, 4096)
        answer = await flow.run()
        self.assertEqual(answer, "Checked league only.\n\n" + AGENT_RESEARCH_UNFINISHED)
        self.assertFalse(session.calls[-1][1])
        self.assertEqual(len(session.calls), 2)
        with self.assertRaises(AgentLimitReached):
            await flow.rounder.advance(final=True, allow_tools=False)
        self.assertEqual(len(session.calls), 2)

    async def test_unexpected_tools_on_last_round_never_execute(self):
        flow, session, _, _ = self.flow([plan(), plan(identity="extra")])
        flow.budget.request_limits = RequestLimits(2, 80_000, 12_000, 4096)
        answer = await flow.run()
        self.assertEqual(answer, AGENT_RESEARCH_UNFINISHED)
        self.assertEqual(flow.runner.tool_calls, 1)
        self.assertEqual(len(session.calls), 2)

    async def test_input_exhaustion_does_not_call_provider(self):
        flow, session, _, _ = self.flow([])
        flow.budget.request_limits = RequestLimits(4, 1, 12_000, 4096)
        with self.assertRaises(AgentLimitReached):
            await flow.rounder.advance(final=True, allow_tools=False)
        self.assertEqual(session.calls, [])

    async def test_output_exhaustion_does_not_call_provider(self):
        flow, session, _, _ = self.flow([])
        flow.budget.spent_output = LOOKUP_LIMITS.output_tokens
        with self.assertRaises(AgentLimitReached):
            await flow.rounder.advance(final=True, allow_tools=False)
        self.assertEqual(session.calls, [])

    async def test_failed_round_consumes_reserved_usage(self):
        flow, session, _, usage = self.flow([])
        session.advance = AsyncMock(side_effect=TimeoutError)
        projected = flow.budget.next_input_estimate
        with self.assertRaises(TimeoutError):
            await flow.rounder.advance(max_output_tokens=2048)
        self.assertEqual(flow.budget.spent_rounds, 1)
        self.assertEqual(flow.budget.spent_input, projected)
        self.assertEqual(flow.budget.spent_output, 2048)
        self.assertEqual(usage.attempted_rounds, 1)

    async def test_cancelled_round_consumes_reserved_usage(self):
        flow, session, _, _ = self.flow([])
        session.advance = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await flow.rounder.advance(max_output_tokens=2048)
        self.assertEqual(flow.budget.spent_rounds, 1)
        self.assertEqual(flow.budget.spent_output, 2048)

    async def test_confirmation_survives_budget_exhaustion_after_preparation(self):
        execute = AsyncMock()
        async def prepare(context, arguments):
            context.state.proposed_changes.append(PreparedAction(
                "change", {}, ChangePreview(("Synthetic change",), AsyncMock(return_value=True)),
                execute,
            ))
            return {"status": "confirmation_required"}
        registry = {
            "read_clash": RegisteredAgentTool(AgentToolDefinition(
                "read_clash", "Read a fact.", {"type": "object", "properties": {}},
            ), AsyncMock(return_value={"value": 7})),
            "change": RegisteredAgentTool(AgentToolDefinition(
                "change", "Prepare a change.", {"type": "object", "properties": {}},
            ), prepare, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE),
        }
        mixed = replace(_model_step(_plan([
            {**_step("read"), "capability": "read_clash"},
            {**_step("change"), "capability": "change"},
        ])), usage=AgentUsage(1000, 100))
        context = replace(_context(), bot=SimpleNamespace(tree=object()))
        with patch("elbow_helper.features.agent.engine.service.build_command_tools", return_value=({}, {})):
            flow, session, _, _ = self.flow(
                [mixed], question="Read and change", registry=registry, context=context,
            )
        # The model's plan consumed the last output tokens. A preview must never
        # turn into a completed action merely because synthesis cannot run.
        advance = session.advance
        async def exhaust(*args, **kwargs):
            step = await advance(*args, **kwargs)
            return replace(step, usage=AgentUsage(1000, RESEARCH_LIMITS.output_tokens))
        session.advance = exhaust
        # Allow preparation to test exhaustion specifically at the feedback call.
        with patch.object(ContextBudget, "can_continue_tools", return_value=True):
            answer = await flow.run()
        self.assertIn(AGENT_RESEARCH_UNFINISHED, answer)
        self.assertIn("Synthetic change", context.state.preview_reply)
        self.assertEqual(len(context.state.proposed_changes), 1)
        execute.assert_not_awaited()
        self.assertEqual(len(session.calls), 1)


class RequestBudgetTests(unittest.TestCase):
    def test_missing_usage_spends_full_allowance_and_reported_cache_hits_count(self):
        budget = ContextBudget(None, 200, 100, request_limits=LOOKUP_LIMITS)
        budget.observe(AgentUsage(), projected_input=100)
        budget.observe(AgentUsage(1000, 50, 900, 100), projected_input=500)
        self.assertEqual((budget.spent_input, budget.spent_output, budget.spent_rounds),
                         (1100, 250, 2))

    def test_expansion_changes_allowances_without_resetting_usage(self):
        budget = ContextBudget(None, 200, 100, request_limits=LOOKUP_LIMITS)
        budget.observe(AgentUsage(1000, 50), projected_input=500)
        budget.request_limits = RESEARCH_LIMITS
        self.assertEqual((budget.spent_input, budget.spent_output, budget.spent_rounds),
                         (1000, 50, 1))
