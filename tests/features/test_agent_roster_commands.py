"""Roster command previews use the public roster workflow."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.commands.adapters.rosters import (
    prepare_roster_clone, prepare_roster_create, roster_adapters,
)
from elbow_helper.features.rosters.cog import Rosters


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

    async def test_clone_previews_copied_settings_and_starts_closed(self):
        source = SimpleNamespace(
            id=4, guild_id=5, name="Source", clan_code="BEH",
            role_id=None, max_members=40, min_townhall=15,
            buttons_hidden=True, schedule_enabled=True,
            schedule_utc_offset="Europe/Paris", open_day="last-2",
            open_time="11:00", close_day="last-1", close_time="20:00",
            reset_on_open=True,
        )
        clone = SimpleNamespace(id=8, name="New Roster")
        workflow = SimpleNamespace(
            validate_roster_name=lambda name: name.strip(),
            roster_name_available=AsyncMock(return_value=True),
            get_roster=AsyncMock(side_effect=lambda row_id: source if row_id == 4 else clone),
            roster_clone_settings=lambda source, **kwargs: Rosters.roster_clone_settings(
                None, source, **kwargs,
            ),
            clone_roster=AsyncMock(return_value=clone),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(id=5, get_role=lambda _: None),
        )
        values = {"roster": "4", "name": "New Roster", "min_townhall": 0}
        prepared = await prepare_roster_clone(context, values)
        self.assertTrue(await prepared.preview.recheck())
        self.assertIn("Minimum Town Hall: None", prepared.preview.lines)
        self.assertIn("The new roster starts closed with no signups.",
                      prepared.preview.lines)
        self.assertEqual((await prepared.run()).result["roster_id"], 8)
        workflow.clone_roster.assert_awaited_once_with(
            source, name="New Roster", clan_code=None, role_id=None,
            max_members=None, min_townhall=0,
        )


if __name__ == "__main__":
    unittest.main()
