"""Keep dependency direction and application-owned resources explicit."""
import ast
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

import discord
from discord import app_commands
from discord.ext import commands

from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.core.lifecycle import REQUIRED_EXTENSIONS
from elbow_helper.infrastructure.discord import command_registry

ROOT = Path(__file__).resolve().parents[2]


def imports(tree, package):
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split('.')[0]] = item.name if item.asname else item.name.split('.')[0]
                yield item.name, node, aliases
        elif isinstance(node, ast.ImportFrom):
            name = '.' * node.level + (node.module or '')
            parent = importlib.util.resolve_name(name, package) if node.level else name
            for item in node.names:
                full = parent + '.' + item.name
                aliases[item.asname or item.name] = full
                yield full, node, aliases


def qualified(node, aliases):
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return qualified(node.value, aliases) + '.' + node.attr
    return ''


class ArchitectureRules(unittest.TestCase):
    def test_dependency_direction_and_resource_owners(self):
        for path in (ROOT / 'elbow_helper').rglob('*.py'):
            relative = path.relative_to(ROOT).with_suffix('')
            package = '.'.join(relative.parts[:-1])
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            imported = list(imports(tree, package))
            aliases = imported[-1][2] if imported else {}
            layer = relative.parts[1] if len(relative.parts) > 2 else ''
            forbidden = {'agent'} if layer == 'features' else set()
            if layer in {'core', 'domain', 'infrastructure', 'data', 'configuration'}:
                forbidden = {'agent', 'features'}
            with self.subTest(path=str(relative)):
                for name, _, _ in imported:
                    self.assertFalse(any(name == f'elbow_helper.{part}' or
                                         name.startswith(f'elbow_helper.{part}.') for part in forbidden), name)
                for node in ast.walk(tree):
                    name = qualified(node, aliases)
                    if isinstance(node, ast.Call) and qualified(node.func, aliases) in {
                            'aiohttp.ClientSession', 'aiohttp.client.ClientSession'}:
                        self.assertEqual(relative.parts[1:3], ('infrastructure', 'clash'))
                    if name in {'os.environ', 'os.getenv', 'os.getenvb', 'os.environb', 'os.putenv', 'os.unsetenv'}:
                        self.assertEqual(relative.as_posix(), 'elbow_helper/core/settings')

    def test_required_extensions_import_and_follow_declared_cog_dependencies(self):
        modules = [importlib.import_module(name) for name in REQUIRED_EXTENSIONS]
        providers = {}
        for index, module in enumerate(modules):
            for value in vars(module).values():
                if isinstance(value, type) and issubclass(value, commands.Cog):
                    providers[value.__cog_name__] = index
        for index, module in enumerate(modules):
            tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                args = node.args
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'get_cog':
                    dependency = args[0] if args else None
                elif isinstance(node.func, ast.Name) and node.func.id == '_require_cog':
                    dependency = args[1] if len(args) > 1 else None
                else:
                    continue
                if isinstance(dependency, ast.Constant) and isinstance(dependency.value, str):
                    with self.subTest(extension=module.__name__, dependency=dependency.value):
                        self.assertLess(providers[dependency.value], index)


class SharedCommandRoots(unittest.IsolatedAsyncioTestCase):
    async def test_build_and_teardown_are_repeatable(self):
        from elbow_helper.features.clan_transfers.cog import ClanTransfers
        from elbow_helper.features.cwl.cog import CwlManagement
        from elbow_helper.features.rosters.cog import Rosters

        client = discord.Client(intents=discord.Intents.none())
        cogs = {cls.__cog_name__: object.__new__(cls) for cls in (ClanTransfers, CwlManagement, Rosters)}
        bot = SimpleNamespace(tree=app_commands.CommandTree(client), get_cog=cogs.get)
        guild = discord.Object(id=GUILD_ID)
        try:
            for _ in range(2):
                await command_registry.setup(bot)
                for name in command_registry.ROOT_NAMES:
                    group = bot.tree.get_command(name, guild=guild)
                    self.assertIsInstance(group, app_commands.Group)
                    self.assertTrue(group.commands)
                await command_registry.teardown(bot)
                for name in command_registry.ROOT_NAMES:
                    self.assertIsNone(bot.tree.get_command(name, guild=guild))
        finally:
            await client.close()
