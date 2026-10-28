"""Synthetic regressions for the run-polish-b3 failure paths."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.access import AgentAccessLost, assert_answer_scope
from elbow_helper.agent.actions.contracts import ActionClass
from elbow_helper.agent.engine.budgets import ContextBudget, RequestLimits
from elbow_helper.agent.engine.grounding import ground_answer
from elbow_helper.agent.engine.named_replies import named_reply
from elbow_helper.agent.engine.rounds import AgentLimitReached
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.agent.plan.checker import check_plan
from elbow_helper.agent.plan.format import capability_list
from elbow_helper.agent.plan.planning import reuse_reply_target
from elbow_helper.agent.prompts import LIMIT_ANSWER_INSTRUCTION
from elbow_helper.infrastructure.ai import AgentToolDefinition, AgentUsage
from agent.engine.test_agent_plan_flow import _context, _model_step, _plan
from agent.engine import test_request_cost as cost

Session, plan, response = cost.Session, cost.plan, cost.response


class PolishReplyTests(unittest.IsolatedAsyncioTestCase):
    flow = cost.RequestCostTests.flow

    async def test_replied_question_finishes_after_reads_and_repeated_reply_target(self):
        context = _context()
        context.state.local_messages = [{"message_id": 101, "author_id": 202, "author": "Wando",
            "content": "Where are the family clan tags?", "source": "https://discord.com/channels/3/91/101"}]
        read = AsyncMock(return_value={"sections": [{"section_id": "clan-links",
            "title": "Clan links", "body": "Find family clan tags in the clans information channel."}]})
        async def target(ctx, arguments):
            ctx.state.reply_to = arguments["message_id"]
            return {"reply_to": arguments["message_id"]}
        target_handler = AsyncMock(side_effect=target)
        registry = {
            "read_clash": RegisteredAgentTool(AgentToolDefinition(
                "read_clash", "Read approved clan links.", {"type": "object", "properties": {}}), read),
            "set_discord_reply_target": RegisteredAgentTool(AgentToolDefinition(
                "set_discord_reply_target", "Set the answer target.", {"type": "object", "properties": {
                    "message_id": {"type": "integer"}, "ping_author": {"type": "boolean"}}}),
                target_handler, action_class=ActionClass.OUTPUT),
        }
        target_step = {"id": "reply", "capability": "set_discord_reply_target",
                       "arguments": {"message_id": 101, "ping_author": False}, "depends_on": []}
        first = _model_step(_plan([{"id": "links", "capability": "read_clash",
                                  "arguments": {}, "depends_on": []}, target_step]))
        final_text = "The family clan tags are in the clans information channel."
        final = _model_step({**_plan([target_step]), "reply": final_text})
        flow, session, _, _ = self.flow([first, final], context=context, registry=registry,
            question="Can you answer their question and point them to the right channel?")
        self.assertEqual(await flow.run(), final_text)
        self.assertEqual(context.state.reply_to, 101)
        self.assertEqual(len(session.calls), 2)
        target_handler.assert_awaited_once()
        read.assert_awaited_once()

    def test_reply_target_reuse_does_not_omit_changes_or_changed_targets(self):
        for name, changed in (("set_discord_reply_target", True), ("add_discord_roles", False)):
            step = {"id": "target", "capability": name, "arguments": {"id": 1}, "depends_on": []}
            revised = {**step, "arguments": {"id": 2}} if changed else step
            self.assertEqual(reuse_reply_target({"reply": "Ready.", "steps": [revised]},
                                               {"target": step})["steps"], [revised])

    def test_old_reply_target_is_not_reused_after_another_target_was_selected(self):
        step = {"id": "target", "capability": "set_discord_reply_target",
                "arguments": {"message_id": 101}, "depends_on": []}
        revised = {"reply": "For the original member.", "steps": [step]}
        self.assertEqual(reuse_reply_target(revised, {"target": step}, reply_to=202), revised)
        self.assertEqual(reuse_reply_target(revised, {"target": step}, reply_to=101)["steps"], [])

    async def test_failed_reply_target_does_not_deliver_supplied_text_to_the_wrong_member(self):
        tool = RegisteredAgentTool(AgentToolDefinition(
            "set_discord_reply_target", "Select the answer target.", {"type": "object", "properties": {
                "message_id": {"type": "integer"}}}), AsyncMock(return_value={"error": "Message unavailable."}),
            action_class=ActionClass.OUTPUT)
        first = _model_step({**_plan([{"id": "target", "capability": "set_discord_reply_target",
            "arguments": {"message_id": 101}, "depends_on": []}]), "reply": "For the other member."})
        flow, session, _, _ = self.flow([first, response("Which member should I answer?")],
            registry={"set_discord_reply_target": tool})
        self.assertEqual(await flow.run(), "Which member should I answer?")
        self.assertIsNone(flow.context.state.reply_to)
        self.assertEqual(len(session.calls), 2)

    async def test_late_repair_links_bind_to_observed_channel_before_scope_guard(self):
        context = _context()
        message = {"message_id": 101, "author_id": 202, "author": "Member",
                   "content": "Review the promotion.", "source": "https://discord.com/channels/3/91/101"}
        evidence = {"history": {"messages": [message]}}
        bad = "https://discord.com/channels/3/92/101"
        draft = f"Member suggested a review ([message]({bad})). Correction: {message['source']}"
        answer = ground_answer(draft, evidence)
        self.assertNotIn(bad, answer)
        self.assertEqual(answer.count(message["source"]), 2)
        with (patch("elbow_helper.agent.access.require_evidence_access", new_callable=AsyncMock),
              patch("elbow_helper.agent.access.accessible_message_channel", new=AsyncMock(
                  side_effect=lambda _, channel_id, **kwargs: context.source_message.channel if channel_id == 91 else None))):
            await assert_answer_scope(context, answer)

    async def test_scope_repair_cannot_leave_the_mistyped_citation_in_its_answer(self):
        message = {"message_id": 101, "author_id": 202, "author": "Member",
                   "content": "Review the promotion.", "source": "https://discord.com/channels/3/91/101"}
        registry = {"read_clash": RegisteredAgentTool(AgentToolDefinition(
            "read_clash", "Read discussion messages.", {"type": "object", "properties": {}}),
            AsyncMock(return_value={"messages": [message]}))}
        bad = "https://discord.com/channels/3/92/101"
        repaired = f"A member suggested a review ([message]({bad})). Correction: {message['source']}"
        flow, session, _, _ = self.flow([plan(), response("The ticket room isn't visible here."),
                                        response(repaired)], registry=registry)
        answer = await flow.run()
        self.assertNotIn(bad, answer)
        self.assertIn(message["source"], answer)
        await assert_answer_scope(flow.context, answer)
        self.assertEqual(len(session.calls), 3)

    def test_named_reply_keeps_links_and_mentions_but_removes_bare_member_role_ids(self):
        context = _context()
        member_id, role_id, other_role = 840568206958657577, 1134542148302282792, 1209086878515732620
        context.state.grounding_evidence = {"roles": {"role_id": role_id, "role_name": "Elder",
            "members": [{"member_id": member_id, "display_name": "Ahmad"}]}}
        context.guild.get_role = lambda identity: SimpleNamespace(id=other_role, name="Elder BEH") if identity == other_role else None
        text = (f"**Ahmad** (`{member_id}`) already has **Elder** (`{role_id}`). "
                f"Other role: `{other_role}`. Source: https://discord.com/channels/3/91/{member_id} <@{member_id}>")
        answer = named_reply(text, context)
        self.assertIn("**Ahmad** already has **Elder**.", answer)
        self.assertIn("Other role: Elder BEH.", answer)
        self.assertIn(f"https://discord.com/channels/3/91/{member_id}", answer)
        self.assertIn(f"<@{member_id}>", answer)
        self.assertNotIn(f"`{role_id}`", answer)

    def test_catalogue_exposes_real_retained_page_limit(self):
        from elbow_helper.agent.engine.registry import build_agent_tools
        text = capability_list(build_agent_tools(), include_returns=False)
        self.assertIn("member_lifecycle:int[1..25]", text)
        self.assertIn("platform:str(length>=1)", text)
        self.assertIn("Omit platform for all sources", text)

    def test_reply_text_still_cannot_claim_a_change_alongside_change_steps(self):
        tool = RegisteredAgentTool(AgentToolDefinition("change", "Prepare a change.",
            {"type": "object", "properties": {}}), AsyncMock(), action_class=ActionClass.CHANGE)
        checked = check_plan({**_plan([{"id": "change", "capability": "change",
            "arguments": {}, "depends_on": []}]), "reply": "Changed."}, {"change": tool})
        self.assertFalse(checked.ok)


class PolishSynthesisTests(unittest.IsolatedAsyncioTestCase):
    flow = cost.RequestCostTests.flow

    async def test_reasoning_only_output_limit_uses_fresh_synthesis_with_all_evidence(self):
        flow, original, _, _ = self.flow([
            replace(plan(), usage=AgentUsage(31_000, 1400)),
        ], question="Recommend a reliable 15-account lineup and flag missing seasons.")
        writer = Session([replace(response(""), usage=AgentUsage(52_000, 8192),
                                  output_limit_reached=True)], [])
        final_text = "BEC has the strongest observed roster. Its missing September history prevents confirming 15 reliable accounts."
        final = Session([response(final_text)], [])
        factory = flow.final_session_factory = Mock(side_effect=[writer, final])
        answer = await flow.run()
        self.assertEqual(answer, final_text)
        self.assertEqual(len(original.calls), 1)
        self.assertEqual(len(writer.calls), 1)
        self.assertTrue(writer.calls[0][1])
        self.assertEqual(len(final.calls), 1)
        self.assertFalse(final.calls[0][1])
        self.assertEqual(factory.call_args.kwargs["tools"], ())
        self.assertIn("Synthetic league", factory.call_args.kwargs["prompt"])
        self.assertNotIn("<capabilities>", factory.call_args.kwargs["system_prompt"])
        self.assertIn("Do not dump knowledge sections", factory.call_args.kwargs["system_prompt"])
        from elbow_helper.agent.conversation.context import estimate_tokens
        self.assertLess(estimate_tokens(factory.call_args.kwargs["system_prompt"]
                                        + factory.call_args.kwargs["prompt"]), 31_000)
        self.assertLess(flow.budget.spent_input, 100_000)

    async def test_tool_cap_finishes_without_resending_catalogue_or_pending_plan_calls(self):
        flow, original, _, _ = self.flow([plan()])
        context_text = "Replied member asks about clan placement; keep the accepted cohort and dates."
        flow.answer_context = context_text
        flow.context.state.local_messages = [{"message_id": 101, "author_id": 202, "author": "Member",
            "content": "Which clan fits?", "source": "https://discord.com/channels/3/91/101"}]
        flow.context.state.grounding_evidence["local_context"] = {"messages": flow.context.state.local_messages}
        final = Session([response("Current roster confirmed; historical attendance remains unverified.")], [])
        factory = flow.final_session_factory = Mock(return_value=final)
        with patch("elbow_helper.agent.engine.flow.limits.MAX_TOOL_CALLS", 1):
            answer = await flow.run()
        self.assertIn("historical attendance remains unverified", answer)
        self.assertEqual(len(original.calls), 1)
        self.assertEqual(final.calls[0][0], ())
        self.assertIn(context_text, factory.call_args.kwargs["prompt"])
        self.assertIn("Which clan fits?", factory.call_args.kwargs["prompt"])

    async def test_synthesis_shares_caps_and_cannot_run_another_round_after_exhaustion(self):
        flow, original, _, _ = self.flow([plan()])
        flow.budget.request_limits = RequestLimits(2, 80_000, 12_000, 4096)
        final = Session([response("Supported result only.")], [])
        flow.final_session_factory = Mock(return_value=final)
        self.assertEqual(await flow.run(), "Supported result only.")
        self.assertEqual(flow.budget.spent_rounds, 2)
        with self.assertRaises(AgentLimitReached):
            await flow.rounder.advance(allow_tools=False, final=True)
        self.assertEqual(len(original.calls) + len(final.calls), 2)

    async def test_revocation_before_compact_prompt_submission_aborts_without_a_provider_call(self):
        flow, original, _, _ = self.flow([plan()])
        final = Session([response("Must not be generated.")], [])
        flow.final_session_factory = Mock(return_value=final)
        with (patch("elbow_helper.agent.engine.flow.limits.MAX_TOOL_CALLS", 1),
              patch("elbow_helper.agent.engine.rounds.require_evidence_access", new=AsyncMock(
                  side_effect=[{}, AgentAccessLost("Revoked before synthesis")]))):
            with self.assertRaises(AgentAccessLost):
                await flow.run()
        self.assertEqual(len(original.calls), 1)
        self.assertEqual(final.calls, [])

    def test_research_preserves_a_full_synthesis_round_in_output_budget(self):
        from elbow_helper.agent.engine.budgets import RESEARCH_LIMITS
        budget = ContextBudget(128_000, 8192, 30_000, request_limits=RESEARCH_LIMITS,
                               spent_output=24_000)
        self.assertFalse(budget.request_can_research(30_000))
        self.assertEqual(budget.request_output_limit(30_000, allow_tools=False, final=True), 8000)
        self.assertIn("provisional recommendation", LIMIT_ANSWER_INSTRUCTION)
