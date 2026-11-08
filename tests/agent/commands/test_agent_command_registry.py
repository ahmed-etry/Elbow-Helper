"""Command entries come from registered commands and help."""

import asyncio
import importlib
import inspect
import pkgutil
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from discord import app_commands

from elbow_helper.agent.commands import CommandAdapter, build_command_capabilities
from elbow_helper.agent.commands.bridge import build_command_tools
from elbow_helper.features.help.discovery import DiscoveredCommand, ParameterInfo
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.agent.actions.outcomes import ActionOutcome
from elbow_helper.infrastructure.discord import command_registry
import elbow_helper.features as features
from elbow_helper.features.cwl.cog import CwlManagement
from elbow_helper.features.rosters.cog import Rosters
from elbow_helper.features.clan_transfers.cog import ClanTransfers
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
               "ClanTransfers": ClanTransfers}
    bot = SimpleNamespace(tree=Tree(),
                          get_cog=lambda name: object.__new__(classes[name]))
    asyncio.run(command_registry.setup(bot))
    return discover_commands(bot)


class CommandRegistryTests(unittest.TestCase):


    def test_agent_option_can_use_a_resolved_numeric_key(self):
        path = "/synthetic inspect"
        command = DiscoveredCommand(path, "registered", (
            ParameterInfo("record", "Choose a record.", True, "string"),
        ))
        help_entry = SimpleNamespace(path=path, summary="Inspect a record.",
                                     details="Shows its state.")
        adapter = CommandAdapter(path, "public", AsyncMock(),
                                 option_types=(("record", "integer"),))
        with (patch("elbow_helper.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            capability = build_command_capabilities(object(), (adapter,))[
                "synthetic_inspect"]
        self.assertEqual(capability.definition.parameters["properties"]["record"]["type"],
                         "integer")
        self.assertEqual(command.parameters[0].type_name, "string")

        invalid = CommandAdapter(path, "public", AsyncMock(),
                                 option_types=(("missing", "integer"),))
        with (patch("elbow_helper.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            with self.assertRaises(ValueError):
                build_command_capabilities(object(), (invalid,))


    def test_duplicate_options_and_adapters_are_rejected(self):
        path = "/synthetic"
        command = DiscoveredCommand(path, "registered", (
            ParameterInfo("value", "A value.", True, "integer"),
        ))
        help_entry = SimpleNamespace(path=path, summary="Do something.", details="Use a value.")
        adapter = CommandAdapter(path, "public", AsyncMock(), (
            ParameterInfo("value", "Again.", False, "integer"),
        ))
        with (patch("elbow_helper.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
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
        with (patch("elbow_helper.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, _ = build_command_tools(object(), (adapter,))
        context = SimpleNamespace(state=AgentTurnState())
        result = await tools["synthetic_edit"].handler(
            context, {"target": 7},
        )
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(context.state.proposed_changes, [])
        self.assertEqual(context.state.outcomes[0].missing,
                         ("What should change?",))
        self.assertEqual(context.state.outcomes[0].command_name,
                         "/synthetic edit")
