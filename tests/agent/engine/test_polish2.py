"""Synthetic regressions for the run-polish-b3 failure paths."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.access import AgentAccessLost, assert_answer_scope
from elbow_helper.agent.actions.contracts import ActionClass
from elbow_helper.agent.engine.grounding import ground_answer
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.agent.plan.checker import check_plan
from elbow_helper.agent.plan.planning import reuse_reply_target
from elbow_helper.infrastructure.ai import AgentToolDefinition
from agent.engine.test_agent_plan_flow import _context, _model_step, _plan
from agent.engine import test_request_cost as cost

Session, plan, response = cost.Session, cost.plan, cost.response


class PolishReplyTests(unittest.IsolatedAsyncioTestCase):
    flow = cost.RequestCostTests.flow


    def test_reply_target_reuse_does_not_omit_changes_or_changed_targets(self):
        for name, changed in (("set_discord_reply_target", True), ("add_discord_roles", False)):
            step = {"id": "target", "capability": name, "arguments": {"id": 1}, "depends_on": []}
            revised = {**step, "arguments": {"id": 2}} if changed else step
            self.assertEqual(reuse_reply_target({"reply": "Ready.", "steps": [revised]},
                                               {"target": step})["steps"], [revised])


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


    def test_reply_text_still_cannot_claim_a_change_alongside_change_steps(self):
        tool = RegisteredAgentTool(AgentToolDefinition("change", "Prepare a change.",
            {"type": "object", "properties": {}}), AsyncMock(), action_class=ActionClass.CHANGE)
        checked = check_plan({**_plan([{"id": "change", "capability": "change",
            "arguments": {}, "depends_on": []}]), "reply": "Changed."}, {"change": tool})
        self.assertFalse(checked.ok)


class PolishSynthesisTests(unittest.IsolatedAsyncioTestCase):
    flow = cost.RequestCostTests.flow


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
