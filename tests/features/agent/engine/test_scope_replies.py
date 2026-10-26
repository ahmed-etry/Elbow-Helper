"""Generated privacy claims must match the destination and a usable control."""

from dataclasses import replace
import json
from elbow_helper.features.agent.wording import AGENT_ANSWER_UNFINISHED
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.actions.outcomes import ActionOutcome, command_reply
from elbow_helper.features.agent.engine.budgets import RequestLimits
from elbow_helper.features.agent.knowledge.answers import (
    MEMBER_REPLY_INSTRUCTION, private_check_offered, scoped_answer, scope_reply_issue,
)
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition
from features.agent.engine import test_request_cost as cost
from features.agent.engine.test_agent_plan_flow import _context


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

    def test_generated_retrieval_narration_requires_repair_but_plain_limits_do_not(self):
        context = _context()
        for text in ("The ticket room itself isn't visible in exam-room.",
                     'A search for "application decision" comes back empty.',
                     "Nothing about application decisions in what I can read.",
                     "I searched the last three weeks and found nothing."):
            for answer in (text, envelope(text, False), json.dumps({"answer": text})):
                with self.subTest(answer=answer):
                    self.assertEqual(MEMBER_REPLY_INSTRUCTION, scope_reply_issue(context, answer))
        for text in ("The promotion ticket's contents aren't available here.",
                     "The available evidence doesn't establish a application decision.",
                     "Promotion exams are for Examiners.",
                     "Ask in <#123> for placement help.",
                     '> A search for "application decision" comes back empty.',
                     '```\nA search for "application decision" comes back empty.\n```'):
            self.assertFalse(scope_reply_issue(context, text), text)

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

    def test_missing_callback_and_requester_only_delivery_never_offer_another_private_check(self):
        context = _context()
        context.state.scope_audit.private_candidates.add("restricted")
        context.state.scope_audit.offered = True
        self.assertTrue(scope_reply_issue(context, envelope(CLAIM)))
        self.assertFalse(private_check_offered(context))
        context.state.private_answer_run = AsyncMock()
        for private in (replace(context, private_pass=True), replace(context, dm_delivery=True)):
            self.assertTrue(scope_reply_issue(private, envelope(CLAIM)))
            self.assertFalse(private_check_offered(private))

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

    async def test_failed_narration_repair_preserves_the_authored_answer(self):
        registry = {"read_clash": tool("read_clash", {"members": [{"display_name": "North"}]})}
        narration = 'A search for "application decision" comes back empty.'
        flow, session, _, _ = self.flow([
            cost.plan(), cost.response(narration), cost.response(narration),
        ], registry=registry)
        answer = await flow.run()
        self.assertEqual(answer, narration)
        self.assertNotIn('North', answer)
        self.assertEqual(len(session.calls), 3)
        registry["read_clash"].handler.assert_awaited_once()

    async def test_visible_hibernation_and_join_dates_are_shown_after_one_answer_only_repair(self):
        members = [{"member_id": n, "display_name": f"Member {n:02}",
                    "joined_at": f"2024-01-{n:02}T00:00:00+00:00"} for n in range(1, 11)]
        registry = {
            "read_active_hibernation": tool("read_active_hibernation", {
                "kind": "active_hibernation", "records": [{"member_id": n,
                "started_ts": 1_700_000_000 + n, "started_at": f"2025-01-{n:02}T00:00:00+00:00"}
                for n in range(1, 11)]}),
            "read_discord_members": tool("read_discord_members", {"members": members}),
        }
        final = "\n".join(f"{m['display_name']}: joined {m['joined_at']}" for m in members)
        flow, session, _, _ = self.flow([
            cost.plan("read_active_hibernation", "hib"),
            cost.plan("read_discord_members", "members"),
            cost.response(envelope(CLAIM)), cost.response(final),
        ], question="Show the ten longest hibernating members and their Discord join dates", registry=registry)
        self.assertEqual(await flow.run(), final)
        self.assertFalse(flow.context.state.scope_audit.excluded)
        self.assertFalse(flow.context.state.scope_audit.offered)
        self.assertEqual(len(session.calls), 4)
        self.assertFalse(session.calls[-1][1])
        self.assertIn("Show the requested findings already returned", session.instructions[-1])
        for capability in registry.values():
            capability.handler.assert_awaited_once()

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
