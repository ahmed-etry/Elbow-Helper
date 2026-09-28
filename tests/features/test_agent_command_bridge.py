"""Command plans invoke adapters and expose only status to the model."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.commands.bridge import build_command_tools, check_command_plan
from elbow_helper.features.agent.commands.outcomes import CommandOutcome
from elbow_helper.features.agent.commands.registry import CommandAdapter
from elbow_helper.features.agent.models import AgentTurnState, AgentCapabilityEffect
from elbow_helper.features.help.discovery import DiscoveredCommand, ParameterInfo


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
        self.assertEqual(context.state.command_outcomes[0].missing, "target")
        self.run.assert_not_awaited()

    async def test_private_content_never_enters_model_result(self):
        with self.patches[0], self.patches[1]:
            tools, _ = build_command_tools(object(), (self.adapter,))
        context = SimpleNamespace(state=AgentTurnState())
        result = await next(iter(tools.values())).handler(context, {"target": 101})
        self.assertEqual(result, {"command": self.path, "status": "complete", "visibility": "private"})
        self.assertNotIn("private synthetic data", str(result))
        self.assertEqual(context.state.command_outcomes[0].private_parts, ("private synthetic data",))
        self.run.assert_awaited_once_with(context, {"target": 101})

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
