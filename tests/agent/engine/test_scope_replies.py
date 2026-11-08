"""Generated privacy claims must match the destination and a usable control."""

import json
from elbow_helper.agent.conversation.wording import AGENT_ANSWER_UNFINISHED
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.actions.outcomes import ActionOutcome, command_reply
from elbow_helper.agent.engine.budgets import RequestLimits
from elbow_helper.agent.knowledge.answers import private_check_offered, scoped_answer, scope_reply_issue
from elbow_helper.agent.engine.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition
from tests.agent.engine import test_request_cost as cost
from tests.agent.engine.test_agent_plan_flow import _context


CLAIM = "The names and dates only open privately here."


def envelope(answer, needed=True):
    return json.dumps({"needs_private_evidence": needed, "answer": answer})


def tool(name, result):
    return RegisteredAgentTool(AgentToolDefinition(name, "Read synthetic findings.", {
        "type": "object", "properties": {},
    }), AsyncMock(return_value=result))


class ScopeReplyTests(unittest.TestCase):
    def test_unbacked_materiality_flag_preserves_authorized_facts_without_a_private_offer(self):
        context = _context()
        context.state.private_answer_run = AsyncMock()
        answer = envelope('The current roster has seven accounts.')
        self.assertFalse(scope_reply_issue(context, answer))
        self.assertEqual(scoped_answer(context, answer), 'The current roster has seven accounts.')
        self.assertFalse(private_check_offered(context))
        context.state.private_answer_run.assert_not_awaited()


    def test_a_model_signal_cannot_invent_private_evidence_or_a_control(self):
        context = _context()
        context.state.private_answer_run = AsyncMock()
        self.assertTrue(scope_reply_issue(context, envelope(CLAIM)))
        self.assertFalse(private_check_offered(context))
        context.state.scope_audit.private_candidates.add("restricted")
        self.assertFalse(scope_reply_issue(context, envelope(CLAIM)))
        scoped_answer(context, envelope(CLAIM))
        self.assertTrue(private_check_offered(context))
        context.state.private_answer_run.assert_not_awaited()

    def test_plain_private_claim_needs_an_offer_but_restrictions_and_quotes_do_not(self):
        context = _context()
        for answer in (CLAIM, "The list is private-only.", "I can show the names privately."):
            self.assertTrue(scope_reply_issue(context, answer), answer)
        for answer in ("The interview notes are for recruiters.",
                       "Three attacks completed; September defenses are unavailable here.",
                       "The names are not private-only; here are all ten.",
                       "> " + CLAIM, "Example message:\n```\n" + CLAIM + "\n```"):
            self.assertFalse(scope_reply_issue(context, answer), answer)


    def test_real_command_private_result_keeps_its_notice_without_excusing_other_withholding(self):
        context = _context()
        context.state.outcomes.append(ActionOutcome("complete", visibility="private",
                                                   private_parts=("Synthetic private command result",)))
        self.assertFalse(scope_reply_issue(context, command_reply(context.state.outcomes)))
        self.assertTrue(scope_reply_issue(context, CLAIM))
        self.assertTrue(scope_reply_issue(context, envelope(CLAIM)))


class ScopeRepairTests(unittest.IsolatedAsyncioTestCase):
    flow = cost.RequestCostTests.flow

    async def test_narration_is_repaired_once_without_more_research(self):
        final = "The promotion ticket's contents aren't available here."
        flow, session, _, _ = self.flow([
            cost.response(json.dumps({"answer": "The ticket room itself isn't visible in exam-room."})),
            cost.response(json.dumps({"answer": final})),
        ], question="What does the promotion ticket say?")
        self.assertEqual(await flow.run(), final)
        self.assertEqual(len(session.calls), 2)
        self.assertFalse(session.calls[-1][1])
        self.assertEqual(flow.runner.tool_calls, 0)


    async def test_repeated_private_claim_and_exhausted_budget_never_dump_names_and_dates(self):
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                registry = {"read_clash": tool("read_clash", {"members": [{
                    "display_name": "North", "joined_at": "2024-01-02T00:00:00+00:00"}]})}
                flow, session, _, _ = self.flow([
                    cost.plan(), cost.response(envelope(CLAIM)), cost.response(CLAIM),
                ], registry=registry)
                if exhausted:
                    flow.budget.request_limits = RequestLimits(2, 80_000, 12_000, 4096)
                answer = await flow.run()
                self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
                self.assertNotIn("2024-01-02", answer)
                self.assertNotIn("privately", answer)
                self.assertEqual(len(session.calls), 2 if exhausted else 3)

    async def test_eligible_excluded_evidence_keeps_the_offer_without_running_private_reads(self):
        context = _context()
        context.state.scope_audit.excluded["restricted"] = "per_person"
        context.state.scope_audit.private_candidates.add("restricted")
        context.state.private_answer_run = AsyncMock()
        flow, session, _, _ = self.flow([cost.response(envelope(CLAIM))], context=context)
        self.assertEqual(await flow.run(), CLAIM)
        self.assertTrue(private_check_offered(context))
        context.state.private_answer_run.assert_not_awaited()
        self.assertEqual(len(session.calls), 1)

    async def test_a_late_rank_repair_cannot_manufacture_private_only_delivery(self):
        registry = {"read_clash": tool("read_clash", {"source": "clash_api", "kind": "player",
            "items": [{"tag": "#P0", "status": "ok", "data": {"name": "North", "role": "coLeader"}}]})}
        flow, session, _, _ = self.flow([
            cost.plan(), cost.response("North is elder."), cost.response(CLAIM),
            cost.response("North is co-leader."),
        ], registry=registry)
        self.assertEqual(await flow.run(), "North is co-leader.")
        self.assertEqual(len(session.calls), 4)
        self.assertFalse(session.calls[-1][1])
