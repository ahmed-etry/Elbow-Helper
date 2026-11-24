"""Changes and commands through the loop: prepared only, checked, and shown for Confirm/Cancel."""

from contextlib import ExitStack
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.actions.contracts import ActionClass, ChangePreview, PreparedAction
from elbow_helper.agent.actions.preview import preview_text
from elbow_helper.agent.engine.models import AgentCapabilityEffect, AgentTurnState, RegisteredAgentTool
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.loop import service as loop
from elbow_helper.agent.loop.service import REFUSED_CHANGE_NOTE
from elbow_helper.infrastructure.ai import AgentToolDefinition
from tests.agent.request_fixtures import flow_context as _context
from tests.agent.loop_stub import StubModel, StubSession, call, final, loop_service, without_server_notes

CATALOGUE = build_agent_tools()
SCHEMA = {"type": "object", "properties": {"target": {"type": "integer"}, "thread_id": {"type": "integer"},
                                           "target_member_id": {"type": "integer"}},
          "additionalProperties": False}


def synthetic(name, action_class=ActionClass.CHANGE, run=None, effect=AgentCapabilityEffect.COMMAND):
    async def prepare(context, arguments):
        context.state.proposed_changes.append(PreparedAction(
            name, dict(arguments), ChangePreview((f"Do {name}",), AsyncMock(return_value=True)),
            run or AsyncMock(), action_class=action_class))
        return {"status": "confirmation_required"}
    return RegisteredAgentTool(AgentToolDefinition(name, "Prepare it.", SCHEMA), prepare, effect, action_class)


class LoopActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.run = AsyncMock()
        self.tools = {"make_thread": synthetic("make_thread", run=self.run),
                      "post_in_thread": synthetic("post_in_thread", run=self.run),
                      "delete_things": synthetic("delete_things", ActionClass.IRREVERSIBLE, self.run)}
        stack = ExitStack()
        self.addCleanup(stack.close)
        without_server_notes(stack)
        stack.enter_context(patch.object(loop, "build_agent_tools", return_value={**CATALOGUE, **self.tools}))
        stack.enter_context(patch.object(loop, "build_command_tools", return_value=({}, {})))
        self.context = replace(_context(), bot=SimpleNamespace(tree=object()), state=AgentTurnState())

    async def answer(self, *steps, context=None):
        session = StubSession(*steps)
        context = context or self.context
        answer = await loop_service(StubModel(session)).answer(question="Synthetic question", context=context)
        return answer, session, context.state

    def request_tools(self, context, *, command_tools=None):
        with patch.object(loop, "build_command_tools", return_value=command_tools or ({}, {})):
            return loop_service(StubModel()).request_tools(context)

    async def test_changes_are_listed_only_for_a_request_that_can_confirm_them(self):
        with patch.object(loop, "role_change_access", return_value={"requester_allowed": True}):
            tools = self.request_tools(self.context)
        self.assertLessEqual({"make_thread", "delete_things", "add_discord_roles"}, set(tools))
        self.assertIn("read_clash", tools)
        for label, context in (("no tree", replace(self.context, bot=SimpleNamespace())),
                               ("private pass", replace(self.context, private_pass=True))):
            with self.subTest(label):
                held = {name for name, tool in self.request_tools(context).items()
                        if tool.action_class in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE)
                        or tool.effect is AgentCapabilityEffect.COMMAND}
                self.assertEqual(held, set())

    async def test_command_tools_follow_the_members_current_roles(self):
        shown = synthetic("shown_command", effect=AgentCapabilityEffect.COMMAND)
        hidden = synthetic("hidden_command")
        role = self.context.member.roles[0].id
        capabilities = {"shown_command": SimpleNamespace(visible_to=frozenset({role})),
                        "hidden_command": SimpleNamespace(visible_to=frozenset({role + 1}))}
        tools = self.request_tools(self.context, command_tools=(
            {"shown_command": shown, "hidden_command": hidden}, capabilities))
        self.assertIn("shown_command", tools)
        self.assertNotIn("hidden_command", tools)

    async def test_role_tools_are_hidden_from_a_member_who_may_not_change_roles(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed), patch.object(
                    loop, "role_change_access", return_value={"requester_allowed": allowed}):
                tools = self.request_tools(self.context)
                self.assertEqual("add_discord_roles" in tools, allowed)
                self.assertEqual("remove_discord_roles" in tools, allowed)

    async def test_a_change_is_prepared_with_its_step_and_nothing_runs(self):
        self.context.source_message.raw_mentions = [7]
        answer, session, state = await self.answer(
            call("make_thread", {"target": 7}), final(""))
        action, = state.proposed_changes
        self.assertEqual((action.step_id, action.capability_name, dict(action.checked_arguments)),
                         ("s1", "make_thread", {"target": 7}))
        self.assertEqual(json.loads(session.calls[1][0].content), {"status": "confirmation_required", "step_id": "s1"})
        self.assertEqual(answer, "")
        self.assertIsNone(state.preview_reply)
        self.assertEqual(state.lookup_results, [])
        self.run.assert_not_awaited()

    async def test_text_and_preview_go_out_together(self):
        self.context.source_message.raw_mentions = [7]
        answer, _, state = await self.answer(call("make_thread", {"target": 7}), final("Ready to go."))
        self.assertEqual(answer, "Ready to go.")
        self.assertEqual(state.preview_reply, preview_text(state.proposed_changes))
        self.assertFalse(state.preview_first)

    async def test_a_change_can_use_the_result_of_an_earlier_one(self):
        answer, session, state = await self.answer(
            call("make_thread", {}),
            call("post_in_thread", {"thread_id": {"step": "s1", "path": ["thread_id"]}}, "call-2"),
            final(""))
        first, second = state.proposed_changes
        self.assertEqual((first.step_id, second.step_id), ("s1", "s2"))
        self.assertEqual(dict(second.checked_arguments), {"thread_id": {"step": "s1", "path": ["thread_id"]}})
        self.assertEqual(json.loads(session.calls[2][0].content)["step_id"], "s2")

    async def test_a_reference_to_a_step_that_was_never_prepared_is_returned_to_the_model(self):
        _, session, state = await self.answer(
            call("post_in_thread", {"thread_id": {"step": "s9", "path": ["thread_id"]}}),
            final("Corrected."))
        self.assertIn("error", json.loads(session.calls[1][0].content))
        self.assertEqual(state.proposed_changes, [])

    async def test_a_mixed_bundle_is_refused_to_the_model_and_the_first_change_stays(self):
        self.context.source_message.raw_mentions = [7]
        answer, session, state = await self.answer(
            call("delete_things", {"target": 7}), call("make_thread", {"target": 7}, "call-2"), final(""))
        self.assertEqual([action.path for action in state.proposed_changes], ["delete_things"])
        refusal = json.loads(session.calls[2][0].content)
        self.assertIn("Only irreversible changes of the same kind", refusal["error"])
        self.assertNotIn("step_id", refusal)
        self.assertEqual(refusal["note"], REFUSED_CHANGE_NOTE)
        self.assertEqual(answer, "")

    async def test_a_change_aimed_at_an_unresolved_member_is_refused_before_it_prepares(self):
        _, session, state = await self.answer(call("make_thread", {"target_member_id": 7}), final("Which one?"))
        refusal = json.loads(session.calls[1][0].content)
        self.assertIn("no target provenance", refusal["error"])
        self.assertNotIn("note", refusal)
        self.assertEqual(state.proposed_changes, [])
        self.context.source_message.raw_mentions = [7]
        _, _, state = await self.answer(call("make_thread", {"target_member_id": 7}), final(""),
                                        context=replace(self.context, state=AgentTurnState()))
        self.assertEqual(len(state.proposed_changes), 1)

    async def test_the_asker_is_a_resolved_member_without_mentioning_themselves(self):
        asker = self.context.member.id
        _, session, state = await self.answer(call("make_thread", {"target_member_id": asker}), final(""))
        self.assertEqual(len(state.proposed_changes), 1)
        self.assertNotIn("error", json.loads(session.calls[1][0].content))

    async def test_commands_use_the_command_timeout_and_the_registered_class(self):
        with patch.object(loop, "execute_tool", AsyncMock(return_value="{}")) as execute:
            await self.answer(call("make_thread", {}), final("Done."))
        self.assertEqual(execute.await_args.kwargs["action_class"], ActionClass.CHANGE)
        self.assertEqual(execute.await_args.kwargs["timeout_seconds"], loop.limits.COMMAND_TIMEOUT_SECONDS)

    async def test_a_command_result_is_added_to_the_reply(self):
        async def run_command(context, arguments):
            from elbow_helper.agent.actions.outcomes import ActionOutcome
            context.state.outcomes.append(ActionOutcome("complete", visibility="public", text="Synthetic outcome."))
            return {"command": "synthetic", "status": "complete"}
        tool = RegisteredAgentTool(AgentToolDefinition("run_command", "Run it.", SCHEMA), run_command,
                                   AgentCapabilityEffect.COMMAND, ActionClass.READ)
        with patch.object(loop, "build_agent_tools", return_value={**CATALOGUE, "run_command": tool}), \
                patch.object(loop, "build_command_tools", return_value=({"run_command": tool}, {
                    "run_command": SimpleNamespace(visible_to=None)})):
            answer, _, state = await self.answer(call("run_command", {}), final(""))
        self.assertIn("Synthetic outcome.", answer)
        self.assertEqual(state.lookup_results, [])
