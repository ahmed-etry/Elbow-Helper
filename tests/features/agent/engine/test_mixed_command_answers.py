"""Command outcomes cannot replace an independently requested read answer."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.contracts import ActionClass, ChangePreview, PreparedAction
from elbow_helper.features.agent.actions.outcomes import ActionOutcome
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.grounding import GROUNDING_GAPS
from elbow_helper.features.agent.models import AgentCapabilityEffect, RegisteredAgentTool
from elbow_helper.features.agent.wording import ACTION_PRIVATE_NOTE, ACTION_PRIVATE_ANSWER
from elbow_helper.infrastructure.ai import AgentStep, AgentToolDefinition, AgentUsage
from features.agent.engine.test_agent_plan_flow import _context, _plan, _step, _model_step, _Model, _Session
from features.agent.engine.test_factual_answers import clan
from features.agent.actions import test_private_answer as private


class MixedAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def answer(self, *, question="Read a value and run the command", payload=None,
                     outcome=None, revised=False, preview=False, command_only=False,
                     read_effect=AgentCapabilityEffect.READ):
        context = _context()
        outcome = outcome or ActionOutcome("complete", text="Command result")

        async def output(local, arguments):
            local.state.outcomes.append(outcome)
            return {"status": outcome.status, "visibility": outcome.visibility}

        async def change(local, arguments):
            local.state.proposed_changes.append(PreparedAction(
                "change_value", {}, ChangePreview(("Synthetic preview",), AsyncMock(return_value=True)),
                AsyncMock(),
            ))
            return {"status": "confirmation_required"}

        registry = {
            "read_value": RegisteredAgentTool(AgentToolDefinition(
                "read_value", "Read synthetic values.", {"type": "object", "properties": {}}),
                AsyncMock(return_value=payload if payload is not None else {"value": 7}), effect=read_effect),
            "command": RegisteredAgentTool(AgentToolDefinition(
                "command", "Run a synthetic command.", {"type": "object", "properties": {}}),
                output, AgentCapabilityEffect.COMMAND, ActionClass.OUTPUT),
            "change_value": RegisteredAgentTool(AgentToolDefinition(
                "change_value", "Change synthetic values.", {"type": "object", "properties": {}}),
                change, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE),
        }
        read = _step("read")
        commands = [{**_step("output"), "capability": "command"}]
        if preview:
            commands.append({**_step("change"), "capability": "change_value"})
        plans = ([_plan([read]), _plan(commands)] if revised else
                 [_plan(commands if command_only else [read, *commands])])
        session = _Session([*(_model_step(plan) for plan in plans),
                            AgentStep("Read answer", (), AgentUsage())], [])
        if preview:
            context = replace(context, bot=SimpleNamespace(tree=object()))
        with (patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry),
              patch("elbow_helper.features.agent.engine.service.build_command_tools", return_value=({}, {}))):
            answer = await AgentService(_Model(session)).answer(question=question, context=context, local_context="")
        return answer, context, session

    async def test_reads_and_public_command_both_keep_their_answers(self):
        answer, context, session = await self.answer()
        self.assertEqual(answer, "Read answer\n\nCommand result")
        self.assertTrue(context.state.has_read_answer)
        self.assertEqual(len(session.calls), 2)
        self.assertNotIn('"output":', session.calls[-1][0][0].content)

    async def test_command_only_does_not_add_a_model_round(self):
        answer, context, session = await self.answer(command_only=True)
        self.assertEqual(answer, "Command result")
        self.assertFalse(context.state.has_read_answer)
        self.assertEqual(len(session.calls), 1)

    async def test_conversation_state_tools_do_not_turn_command_outcomes_into_research(self):
        answer, context, session = await self.answer(read_effect=AgentCapabilityEffect.STATE)
        self.assertEqual(answer, "Command result")
        self.assertFalse(context.state.has_read_answer)
        self.assertEqual(len(session.calls), 1)

    async def test_numeric_grounding_does_not_erase_command_outcome(self):
        answer, _, _ = await self.answer(question="How many members in #P0? Run the command too", payload=clan())
        self.assertEqual(answer, "48\n\nCommand result")

    async def test_strict_count_keeps_command_result_without_free_prose_generation(self):
        answer, _, session = await self.answer(question="Count only; run the command too", payload=clan())
        self.assertEqual(answer, "48\n\nCommand result")
        self.assertEqual(len(session.calls), 1)

    async def test_reads_from_earlier_revision_survive_later_command(self):
        answer, _, session = await self.answer(revised=True)
        self.assertEqual(answer, "Read answer\n\nCommand result")
        self.assertEqual(len(session.calls), 3)
        self.assertIn('"read":', session.calls[-1][0][0].content)

    async def test_preview_command_and_read_all_survive_numeric_rendering(self):
        answer, context, _ = await self.answer(question="Member count? Also run the command and prepare a change",
                                               payload=clan(), preview=True)
        self.assertEqual(answer, "48\n\nCommand result")
        self.assertIn("Synthetic preview", context.state.preview_reply)
        self.assertEqual(len(context.state.proposed_changes), 1)
        context.state.proposed_changes[0].run.assert_not_awaited()

    async def test_private_command_stays_private_alongside_read_answer(self):
        outcome = ActionOutcome("complete", "private", private_parts=("Private command result",))
        answer, _, session = await self.answer(outcome=outcome)
        self.assertEqual(answer, f"Read answer\n\n{ACTION_PRIVATE_NOTE}")
        self.assertNotIn("Private command result", str(session.calls))

    async def test_failed_strict_read_does_not_hide_completed_command(self):
        answer, _, _ = await self.answer(question="count only; run the command", payload={"error": "unavailable"})
        self.assertEqual(answer, f"{GROUNDING_GAPS['count']}\n\nCommand result")


class MixedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        private.PrivateAnswerTests.setUp(self)

    async def test_command_outcome_does_not_bypass_read_audience_check(self):
        self.context.state.outcomes.append(ActionOutcome("complete", text="Command result"))
        self.context.state.has_read_answer = True
        with patch("elbow_helper.features.agent.delivery.can_show", new=AsyncMock(return_value=False)) as audience:
            await self.delivery.send_response(self.message, "Secret answer\n\nCommand result", None,
                                             conversation=self.conversation, context=self.context)
        audience.assert_awaited_once()
        self.assertEqual(self.message.reply.await_args.args[0], ACTION_PRIVATE_ANSWER)
        self.assertNotIn("Secret", str(self.message.reply.await_args))
