"""Command entries come from registered commands and help."""

import asyncio
import importlib
import inspect
import pkgutil
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from discord import app_commands

from elbow_helper.features.agent.commands import CommandAdapter, build_command_capabilities
from elbow_helper.features.agent.commands.bridge import build_command_tools
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.conversation.context import estimate_tokens
from elbow_helper.features.help.discovery import DiscoveredCommand, ParameterInfo
from elbow_helper.features.agent.capabilities import enabled_adapters
from elbow_helper.features.agent.plan.format import capability_list, system_instructions
from elbow_helper.features.agent.models import RegisteredAgentTool, AgentCapabilityEffect
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.actions.outcomes import ActionOutcome
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.recruitment.commands import RecruitmentCommandMixin
from elbow_helper.features.clan_health.commands.health import ClanHealthRootCommandMixin
from elbow_helper.discord import command_registry
import elbow_helper.features as features
from elbow_helper.features.cwl.cog import CwlManagement
from elbow_helper.features.rosters.cog import Rosters
from elbow_helper.features.clan_transfers.cog import ClanTransfers
from elbow_helper.features.records.cog import Records
from elbow_helper.features.help.discovery import discover_commands


def _registered_command_catalogue():
    roots = {}
    for _, name, _ in pkgutil.walk_packages(features.__path__, features.__name__ + "."):
        module = importlib.import_module(name)
        for owner in vars(module).values():
            if inspect.isclass(owner) and owner.__module__ == module.__name__:
                for command in vars(owner).values():
                    if isinstance(command, (app_commands.Command, app_commands.Group)):
                        roots[id(command)] = command

    class Tree:
        def __init__(self):
            self.groups = []

        def get_command(self, *args, **kwargs):
            return None

        def remove_command(self, *args, **kwargs):
            pass

        def add_command(self, command, **kwargs):
            self.groups.append(command)

        def get_commands(self, guild=None):
            return list(roots.values()) if guild is None else self.groups

    classes = {"CwlManagement": CwlManagement, "Rosters": Rosters,
               "ClanTransfers": ClanTransfers, "Records": Records}
    bot = SimpleNamespace(tree=Tree(),
                          get_cog=lambda name: object.__new__(classes[name]))
    asyncio.run(command_registry.setup(bot))
    return discover_commands(bot)


class CommandRegistryTests(unittest.TestCase):
    def test_full_action_catalogue_fits_the_system_prompt_budget(self):
        adapters = enabled_adapters()
        discovered = _registered_command_catalogue()
        registry = build_agent_tools()
        with patch("elbow_helper.features.agent.commands.registry.discover_commands",
                   return_value=discovered):
            commands, _ = build_command_tools(object(), adapters)
        self.assertEqual(len(commands), len(adapters))
        registry.update(commands)
        prompt = system_instructions(registry, actions_enabled=True)
        # Keep the total prompt budget while allowing typed result contracts.
        without_results = "\n".join(
            " | ".join(field for field in line.split(" | ") if not field.startswith("results "))
            for line in prompt.splitlines()
        )
        self.assertLess(estimate_tokens(without_results), 30_000)
        self.assertLess(estimate_tokens(prompt) - estimate_tokens(without_results), 2_000)
        self.assertLess(estimate_tokens(prompt), 32_000)
        self.assertEqual(len(capability_list(registry).splitlines()), len(registry))
        self.assertTrue(all(
            any(" | " + kind + " | " in line
                for kind in ("read", "output", "change", "irreversible"))
            for line in capability_list(registry).splitlines()
        ))
        self.assertTrue(all(
            tool.effect is AgentCapabilityEffect.COMMAND for tool in registry.values()
            if tool.action_class in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE)
        ))

    def test_enabled_adapters_match_the_registered_commands(self):
        bot = SimpleNamespace(tree=SimpleNamespace(get_commands=lambda guild=None: (
            [RecruitmentCommandMixin.slash_opinion, ClanHealthRootCommandMixin.health]
            if guild is not None else []
        )))
        capabilities = build_command_capabilities(bot, enabled_adapters())
        self.assertEqual(set(capabilities), {
            "run_command_opinion", "run_command_health_player",
            "run_command_health_clan", "run_command_health_settings",
        })
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

    def test_agent_option_can_use_a_resolved_numeric_key(self):
        path = "/synthetic inspect"
        command = DiscoveredCommand(path, "registered", (
            ParameterInfo("record", "Choose a record.", True, "string"),
        ))
        help_entry = SimpleNamespace(path=path, summary="Inspect a record.",
                                     details="Shows its state.")
        adapter = CommandAdapter(path, "public", AsyncMock(),
                                 option_types=(("record", "integer"),))
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            capability = build_command_capabilities(object(), (adapter,))[
                "run_command_synthetic_inspect"]
        self.assertEqual(capability.definition.parameters["properties"]["record"]["type"],
                         "integer")
        self.assertEqual(command.parameters[0].type_name, "string")

        invalid = CommandAdapter(path, "public", AsyncMock(),
                                 option_types=(("missing", "integer"),))
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            with self.assertRaises(ValueError):
                build_command_capabilities(object(), (invalid,))

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


class ConfirmedCommandInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_can_ask_for_a_missing_edit_choice(self):
        path = "/synthetic edit"
        command = DiscoveredCommand(path, "registered", (
            ParameterInfo("target", "Choose a target.", True, "integer"),
        ))
        help_entry = SimpleNamespace(path=path, summary="Edit a target.",
                                     details="Changes its values.")
        prepare = AsyncMock(return_value=ActionOutcome.needs_input((
            "What should change?",
        )))
        adapter = CommandAdapter(path, "confirm", AsyncMock(), prepare=prepare)
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, _ = build_command_tools(object(), (adapter,))
        context = SimpleNamespace(state=AgentTurnState())
        result = await tools["run_command_synthetic_edit"].handler(
            context, {"target": 7},
        )
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(context.state.proposed_changes, [])
        self.assertEqual(context.state.outcomes[0].missing,
                         ("What should change?",))
        self.assertEqual(context.state.outcomes[0].command_name,
                         "/synthetic edit")
