"""Command entries come from registered commands and help."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.commands import CommandAdapter, build_command_capabilities
from elbow_helper.features.help.discovery import DiscoveredCommand, ParameterInfo
from elbow_helper.features.agent.commands.adapters import enabled_adapters
from elbow_helper.features.agent.plan.format import capability_list
from elbow_helper.features.agent.models import RegisteredAgentTool, AgentCapabilityEffect
from elbow_helper.features.recruitment.commands import RecruitmentCommandMixin
from elbow_helper.features.clan_health.commands.health import ClanHealthRootCommandMixin


class CommandRegistryTests(unittest.TestCase):
    def test_enabled_adapters_match_the_registered_commands(self):
        bot = SimpleNamespace(tree=SimpleNamespace(get_commands=lambda guild=None: (
            [RecruitmentCommandMixin.slash_opinion, ClanHealthRootCommandMixin.health]
            if guild is not None else []
        )))
        capabilities = build_command_capabilities(bot, enabled_adapters())
        self.assertEqual(set(capabilities), {"run_command_opinion", "run_command_health_player"})
        self.assertEqual(capabilities["run_command_opinion"].required, ("ticket",))
        self.assertEqual(capabilities["run_command_health_player"].required, ("account",))
        self.assertEqual(
            capabilities["run_command_health_player"].definition.parameters["properties"]["period"]["enum"],
            ["last_7d", "last_14d", "last_30d", "custom"],
        )

    def test_registered_options_and_help_define_the_capability(self):
        path = "/synthetic inspect"
        command = DiscoveredCommand(path, "registered description", (
            ParameterInfo("target", "Choose a target.", True, "string"),
            ParameterInfo("mode", "Choose a mode.", False, "string",
                          ("First", "Second"), False, ("first", "second")),
        ))
        help_entry = SimpleNamespace(path=path, summary="Inspect a target.",
                                     details="Shows its current state.")
        adapter = CommandAdapter(path, "public", AsyncMock())
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            capabilities = build_command_capabilities(object(), (adapter,))
        capability = capabilities["run_command_synthetic_inspect"]
        self.assertEqual(capability.required, ("target",))
        catalogue = capability_list({"run_command_synthetic_inspect": RegisteredAgentTool(
            capability.definition, AsyncMock(), AgentCapabilityEffect.COMMAND,
        )})
        self.assertIn("target:string*", catalogue)
        self.assertEqual(capability.definition.description,
                         "Inspect a target. Shows its current state.")
        self.assertEqual(capability.definition.parameters["properties"]["mode"]["enum"],
                         ["first", "second"])
        self.assertIn("First (first)",
                      capability.definition.parameters["properties"]["mode"]["description"])

    def test_unregistered_or_missing_help_commands_stay_hidden(self):
        adapter = CommandAdapter("/synthetic", "public", AsyncMock())
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", ())):
            self.assertEqual(build_command_capabilities(object(), (adapter,)), {})

    def test_duplicate_options_and_adapters_are_rejected(self):
        path = "/synthetic"
        command = DiscoveredCommand(path, "registered", (
            ParameterInfo("value", "A value.", True, "integer"),
        ))
        help_entry = SimpleNamespace(path=path, summary="Do something.", details="Use a value.")
        adapter = CommandAdapter(path, "public", AsyncMock(), (
            ParameterInfo("value", "Again.", False, "integer"),
        ))
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            with self.assertRaises(ValueError):
                build_command_capabilities(object(), (adapter,))
            with self.assertRaises(ValueError):
                build_command_capabilities(object(),
                                           (CommandAdapter(path, "public", AsyncMock()),) * 2)
