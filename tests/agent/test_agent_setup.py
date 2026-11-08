from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent import setup


class AgentSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_collision_stops_setup_before_registration(self):
        from elbow_helper.agent.commands.registry import CommandAdapter
        bot = SimpleNamespace(add_cog=AsyncMock())
        adapters = (CommandAdapter(
            "/synthetic", "public", AsyncMock(), capability_name="read_clash",
        ),)
        with patch(
            'elbow_helper.agent.engine.adapters.enabled_adapters', return_value=adapters,
        ):
            with self.assertRaisesRegex(ValueError, "read_clash"):
                await setup(bot)
        bot.add_cog.assert_not_awaited()


    async def test_missing_dependency_does_not_register_partial_agent(self):
        for missing in (
            "AccountLinks", "ClanHealth", "WarManager", "Rosters",
            "CwlManagement", "ClanTransfers",
            "Hibernate", "SupportActions", "Recruitment", "Examination",
            "Achievements",
            "EventStatsCog",
            "MemberLifecycle",
            "ClanReporting",
            "RoleConnections",
        ):
            with self.subTest(missing=missing):
                dependencies = {
                    "AccountLinks": object(), "ClanHealth": object(),
                    "WarManager": SimpleNamespace(queries=object()),
                    "Rosters": SimpleNamespace(queries=object()),
                    "CwlManagement": SimpleNamespace(queries=object()),
                    "ClanTransfers": SimpleNamespace(queries=object()),
                    "Hibernate": SimpleNamespace(queries=object()),
                    "SupportActions": SimpleNamespace(queries=object()),
                    "Recruitment": SimpleNamespace(queries=object()),
                    "Examination": SimpleNamespace(queries=object()),
                    "Achievements": SimpleNamespace(queries=object()),
                    "EventStatsCog": SimpleNamespace(queries=object()),
                    "MemberLifecycle": SimpleNamespace(queries=object()),
                    "ClanReporting": SimpleNamespace(queries=object()),
                    "RoleConnections": SimpleNamespace(queries=object()),
                }
                dependencies.pop(missing)
                bot = SimpleNamespace(get_cog=dependencies.get, add_cog=AsyncMock())
                with self.assertRaises(RuntimeError):
                    await setup(bot)
                bot.add_cog.assert_not_awaited()
