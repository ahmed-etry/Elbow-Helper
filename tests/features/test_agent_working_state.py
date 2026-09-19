from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.conversation.context import compile_context
from elbow_helper.features.agent.conversation.state import ConversationTurn
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.service import CoreAgentService
from elbow_helper.features.agent.conversation.instructions import WorkingState
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage


def _remember(state, quote="Keep these accounts together", **changes):
    values = dict(label="Account grouping", quote=quote, request_text=quote,
                  member_id=42, message_id=1, channel_id=100, created_at="2026-09-17")
    values.update(changes)
    return state.remember(**values)


class WorkingStateTests(unittest.TestCase):
    def test_instruction_must_be_a_verbatim_current_request_quote(self):
        with self.assertRaises(ValueError):
            _remember(WorkingState(), request_text="Different request")

    def test_replacement_keeps_original_quote_and_revision_source(self):
        original, first = _remember(WorkingState())
        revised, second = _remember(original, "Separate these accounts", message_id=2, replaces_id=first.instruction_id)
        self.assertTrue(original.instructions[0].active)
        self.assertEqual(revised.instructions[0].quote, first.quote)
        self.assertEqual(revised.instructions[0].retired_by_message_id, 2)
        self.assertEqual(second.supersedes_id, first.instruction_id)
        self.assertEqual(revised.version, 2)

    def test_another_member_cannot_replace_or_retire_an_instruction(self):
        state, instruction = _remember(WorkingState())
        with self.assertRaises(ValueError):
            _remember(state, replaces_id=instruction.instruction_id, member_id=43)
        with self.assertRaises(ValueError):
            state.retire(instruction.instruction_id, member_id=43, message_id=2)
        self.assertTrue(state.instructions[0].active)

    def test_duplicate_instruction_is_idempotent(self):
        state, first = _remember(WorkingState())
        duplicate, second = _remember(state, message_id=2)
        self.assertIs(duplicate, state)
        self.assertIs(second, first)

    def test_active_limit_rejects_without_evicting_older_constraints(self):
        state, first = _remember(WorkingState())
        with patch("elbow_helper.features.agent.conversation.instructions.MAX_ACTIVE_INSTRUCTIONS", 1):
            with self.assertRaises(ValueError):
                _remember(state, "Another constraint")
        self.assertEqual(state.instructions, (first,))

    def test_revision_budget_discards_inactive_history_not_active_constraints(self):
        state, first = _remember(WorkingState())
        with patch("elbow_helper.features.agent.conversation.instructions.MAX_INSTRUCTION_REVISIONS", 1):
            revised, second = _remember(state, "New constraint", replaces_id=first.instruction_id)
        self.assertEqual(revised.instructions, (second,))

    def test_byte_budget_rejects_atomically(self):
        state, first = _remember(WorkingState())
        with patch("elbow_helper.features.agent.conversation.instructions.MAX_INSTRUCTION_BYTES", 1):
            with self.assertRaises(ValueError):
                _remember(state, "New constraint", replaces_id=first.instruction_id)
        self.assertEqual(state.instructions, (first,))


class WorkingInstructionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        member = SimpleNamespace(id=42, display_name="Tester", roles=[SimpleNamespace(id=next(iter(CORE)))])
        guild = SimpleNamespace(id=1, name="Brown Elbow", me=member, get_member=lambda _: member)
        channel = SimpleNamespace(id=100, guild=guild, permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
        guild.get_channel_or_thread = lambda _: channel
        return AgentRequestContext(
            bot=None, guild=guild, member=member,
            source_message=SimpleNamespace(id=1, channel=channel, created_at=datetime.now(timezone.utc)),
            account_links=None, clan_health=None, message_search=None,
        )

    async def test_model_loop_remembers_exact_instruction_without_authorizing_actions(self):
        context = self.context()
        session = SimpleNamespace(
            replace_tools=MagicMock(),
            advance=AsyncMock(side_effect=[
            AgentStep("", (AgentToolCall(
                "discover", "discover_agent_tools",
                '{"groups":["planning_output"]}',
            ),), AgentUsage()),
            AgentStep("", (AgentToolCall("record", "remember_task_instruction", '{"label":"Group","quote":"Keep these accounts together"}'),), AgentUsage()),
            AgentStep("I will keep those accounts together in this draft.", (), AgentUsage()),
        ]))
        model = SimpleNamespace(create_agent_session=lambda **kwargs: session)
        await CoreAgentService(model).answer(question="Keep these accounts together", local_context="", context=context)
        self.assertEqual(context.state.working.instructions[0].quote, "Keep these accounts together")
        self.assertIn('"action_authorized": false', session.advance.await_args_list[2].args[0][0].content)

    async def test_pinned_instruction_survives_context_pressure(self):
        context = self.context()
        working, instruction = _remember(WorkingState())
        context.state.working = working
        context.state.authorized_instructions = (instruction,)
        context.state.authorized_history = (ConversationTurn("x" * 50_000, frozenset({200})),)
        compiled = compile_context(question="Prepare the draft", local_context="", context=context, tools=(), target_tokens=4000)
        self.assertIn(instruction.quote, compiled.prompt)
        self.assertEqual(compiled.omitted_turns, 1)
        self.assertEqual(context.state.source_channels, {100})
