"""Roster command previews use the public roster workflow."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.commands.adapters.rosters import (
    prepare_roster_create, roster_adapters,
)


class RosterCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_previews_settings_and_verifies_roster(self):
        roster = SimpleNamespace(id=7, name="War Signup")
        workflow = SimpleNamespace(
            validate_roster_name=lambda name: " ".join(name.split()),
            roster_name_available=AsyncMock(return_value=True),
            create_roster=AsyncMock(return_value=roster),
            get_roster=AsyncMock(return_value=roster),
        )
        guild = SimpleNamespace(id=5, get_role=lambda _: None, me=None)
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow), guild=guild,
        )
        prepared = await prepare_roster_create(context, {
            "name": " War  Signup ", "clan": "BEH", "max_members": 30,
        })
        self.assertEqual(roster_adapters()[0].path, "/roster create")
        self.assertIn("Create roster **War Signup**.", prepared.preview.lines)
        self.assertIn("Maximum accounts: 30", prepared.preview.lines)
        workflow.create_roster.assert_not_awaited()
        self.assertTrue(await prepared.preview.recheck())
        result = await prepared.run()
        self.assertEqual(result.result["roster_id"], 7)
        workflow.create_roster.assert_awaited_once_with(
            guild_id=5, name="War Signup", clan_code="BEH",
            role_id=None, max_members=30,
        )


if __name__ == "__main__":
    unittest.main()
