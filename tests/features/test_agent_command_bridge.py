"""Command plans invoke adapters and expose only status to the model."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.commands.bridge import build_command_tools, check_command_plan
from elbow_helper.features.agent.commands.confirmation import ChangePreview
from elbow_helper.features.agent.commands.outcomes import CommandOutcome, command_reply
from elbow_helper.features.agent.commands.registry import CommandAdapter
from elbow_helper.features.agent.models import AgentTurnState, AgentCapabilityEffect
from elbow_helper.features.agent.models import AgentAttachment
from elbow_helper.features.help.discovery import DiscoveredCommand, ParameterInfo
from elbow_helper.features.agent.wording import COMMAND_EMPTY, COMMAND_PRIVATE_NOTE


class CommandBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.path = "/synthetic"
        self.run = AsyncMock(return_value=CommandOutcome(
            "complete", "private", private_parts=("private synthetic data",),
        ))
        self.adapter = CommandAdapter(
            self.path, "private", self.run,
            entity_options=(("target", "synthetic_source"),),
        )
        command = DiscoveredCommand(self.path, "Registered", (
            ParameterInfo("target", "Choose a target.", True, "integer"),
        ))
        help_entry = SimpleNamespace(path=self.path, summary="Inspect a target.", details="Show its result.")
        self.patches = (
            patch("elbow_helper.features.agent.commands.registry.discover_commands",
                  return_value={self.path: command}),
            patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,)),
        )

    async def test_missing_input_asks_without_running(self):
        with self.patches[0], self.patches[1]:
            tools, _ = build_command_tools(object(), (self.adapter,))
        context = SimpleNamespace(state=AgentTurnState())
        tool = next(iter(tools.values()))
        self.assertIs(tool.effect, AgentCapabilityEffect.COMMAND)
        result = await tool.handler(context, {})
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(context.state.command_outcomes[0].missing, ("Choose a target.",))
        self.assertEqual(command_reply(context.state.command_outcomes),
                         "Which value should I use?\n- Choose a target.")
        self.run.assert_not_awaited()

    async def test_all_missing_values_use_option_descriptions(self):
        command = DiscoveredCommand(self.path, "Registered", (
            ParameterInfo("first_target", "The first target", True, "integer"),
            ParameterInfo("second_target", "The second target", True, "integer"),
        ))
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={self.path: command}), self.patches[1]):
            tools, _ = build_command_tools(object(), (self.adapter,))
        context = SimpleNamespace(state=AgentTurnState())
        await next(iter(tools.values())).handler(context, {})
        self.assertEqual(context.state.command_outcomes[0].missing,
                         ("The first target", "The second target"))

    async def test_false_and_zero_are_valid_required_values(self):
        command = DiscoveredCommand(self.path, "Registered", (
            ParameterInfo("enabled", "Choose a value.", True, "boolean"),
            ParameterInfo("count", "Choose a count.", True, "integer"),
        ))
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={self.path: command}), self.patches[1]):
            tools, _ = build_command_tools(object(), (self.adapter,))
        context = SimpleNamespace(state=AgentTurnState())
        values = {"enabled": False, "count": 0}
        result = await next(iter(tools.values())).handler(context, values)
        self.assertEqual(result["status"], "complete")
        self.run.assert_awaited_once_with(context, values)

    async def test_private_content_never_enters_model_result(self):
        with self.patches[0], self.patches[1]:
            tools, _ = build_command_tools(object(), (self.adapter,))
        context = SimpleNamespace(state=AgentTurnState())
        result = await next(iter(tools.values())).handler(context, {"target": 101})
        self.assertEqual(result, {"command": self.path, "status": "complete", "visibility": "private"})
        self.assertNotIn("private synthetic data", str(result))
        self.assertEqual(context.state.command_outcomes[0].private_parts, ("private synthetic data",))
        self.run.assert_awaited_once_with(context, {"target": 101})

    async def test_private_text_and_files_never_enter_public_delivery_state(self):
        self.run.return_value = CommandOutcome(
            "complete", "private", text="synthetic private text",
            attachments=(AgentAttachment("synthetic.txt", b"private bytes"),),
        )
        with self.patches[0], self.patches[1]:
            tools, _ = build_command_tools(object(), (self.adapter,))
        context = SimpleNamespace(state=AgentTurnState())
        result = await next(iter(tools.values())).handler(context, {"target": 101})
        self.assertNotIn("synthetic private text", str(result))
        self.assertNotIn("private bytes", str(result))
        self.assertEqual(context.state.attachments, [])
        self.assertEqual(context.state.command_outcomes[0].private_parts,
                         ("synthetic private text",))

    async def test_empty_private_result_is_not_posted_in_the_channel(self):
        self.run.return_value = CommandOutcome("empty", "private")
        with self.patches[0], self.patches[1]:
            tools, _ = build_command_tools(object(), (self.adapter,))
        context = SimpleNamespace(state=AgentTurnState())
        result = await next(iter(tools.values())).handler(context, {"target": 101})
        self.assertEqual(result["visibility"], "private")
        self.assertEqual(command_reply(context.state.command_outcomes), COMMAND_PRIVATE_NOTE)
        self.assertEqual(context.state.command_outcomes[0].private_parts, (COMMAND_EMPTY,))

    async def test_named_sources_are_checked_before_execution(self):
        with self.patches[0], self.patches[1]:
            _, capabilities = build_command_tools(object(), (self.adapter,))
        plan = {"periods": [], "steps": [{"capability": next(iter(capabilities)),
                                           "arguments": {"target": 202}}]}
        self.assertTrue(check_command_plan(plan, capabilities,
                                           {"synthetic_source": frozenset({101})}))
        plan["steps"][0]["arguments"]["target"] = 101
        self.assertEqual(check_command_plan(plan, capabilities,
                                            {"synthetic_source": frozenset({101})}), "")

    async def test_confirmed_adapter_only_prepares_before_the_button(self):
        preview = AsyncMock(return_value=ChangePreview(
            ("Change synthetic target",), AsyncMock(return_value=True),
        ))
        adapter = CommandAdapter(self.path, "confirm", self.run, prepare=preview)
        with self.patches[0], self.patches[1]:
            tools, _ = build_command_tools(object(), (adapter,))
        context = SimpleNamespace(state=AgentTurnState())
        result = await next(iter(tools.values())).handler(context, {"target": 101})
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(len(context.state.command_proposals), 1)
        self.assertEqual(context.state.command_proposals[0].preview.lines,
                         ("Change synthetic target",))
        self.run.assert_not_awaited()
        preview.assert_awaited_once_with(context, {"target": 101})
