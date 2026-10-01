"""Keep the agent boundary and command coverage visible across the whole registry."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.commands.adapters import enabled_adapters
from elbow_helper.features.agent.tools import build_agent_tools
from elbow_helper.features.help.catalog import HELP_ENTRIES


EXCLUDED_COMMANDS = frozenset({"/help", "/ping"})

READ_COVERAGE = {
    "/achievements": ("read_member_achievements",),
    "/achievement leaderboard": ("read_achievement_leaderboard",),
    "/inventory": ("read_member_inventory",),
    "/economyinfo": ("read_achievement_economy_rules",),
    "/coinlog": ("read_member_coin_history",),
    "/raffle list": ("read_raffle",),
    "/raffle history": ("read_raffle",),
    "/account list": ("get_linked_accounts", "get_account_link"),
    "/roster list": ("find_rosters", "read_roster"),
    "/event list": ("read_event_schedule",),
    "/recstats": ("read_member_lifecycle",),
}

# Remove each entry when its feature adapter is added.
NOT_YET_ADAPTED = frozenset({
    "/transfer reminder",
})


class AgentRegistryCoverageTests(unittest.TestCase):
    def test_every_help_command_has_one_explicit_route(self):
        help_paths = [entry.path for entry in HELP_ENTRIES]
        adapters = [adapter.path for adapter in enabled_adapters()]
        self.assertEqual(len(help_paths), len(set(help_paths)))
        self.assertEqual(len(adapters), len(set(adapters)))
        covered = set(adapters) | set(READ_COVERAGE) | EXCLUDED_COMMANDS | NOT_YET_ADAPTED
        self.assertEqual(set(help_paths), covered)
        categories = (set(adapters), set(READ_COVERAGE), EXCLUDED_COMMANDS,
                      NOT_YET_ADAPTED)
        for index, first in enumerate(categories):
            for second in categories[index + 1:]:
                self.assertFalse(first & second, first & second)

        tools = build_agent_tools()
        for path, names in READ_COVERAGE.items():
            with self.subTest(path=path):
                self.assertTrue(names)
                for name in names:
                    self.assertIn(name, tools)
                    self.assertIs(tools[name].action_class, ActionClass.READ)

    def test_agent_does_not_use_other_objects_private_attributes(self):
        root = Path(__file__).resolve().parents[2] / "elbow_helper" / "features" / "agent"
        violations = []
        for path in root.rglob("*.py"):
            source = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(source):
                if isinstance(node, ast.Attribute):
                    if (node.attr.startswith("_") and not node.attr.startswith("__")
                            and not _is_own_receiver(node.value)):
                        violations.append(f"{path.relative_to(root)}:{node.lineno}: {ast.unparse(node)}")
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                      and node.func.id in {"getattr", "setattr", "hasattr", "delattr"}
                      and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                      and isinstance(node.args[1].value, str)):
                    name = node.args[1].value
                    if (name.startswith("_") and not name.startswith("__")
                            and not _is_own_receiver(node.args[0])):
                        violations.append(f"{path.relative_to(root)}:{node.lineno}: {ast.unparse(node)}")
        self.assertEqual(violations, [])


def _is_own_receiver(value: ast.expr) -> bool:
    return isinstance(value, ast.Name) and value.id in {"self", "cls"}
