from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import discord

from elbow_helper.features.attack_plans.cog import Planning
from elbow_helper.features.attack_plans.formatting import PlanningEmbeds
from elbow_helper.features.attack_plans.formatting import build_planning_embeds
from elbow_helper.features.attack_plans.unit_levels import TOWN_HALL_MAX_LEVELS
from elbow_helper.features.attack_plans.unit_levels import town_hall_max_level
from elbow_helper.features.attack_plans.views import PlanningView


class _PlanningHarness:
    clan_health = None

    def __init__(self) -> None:
        self.clan_health = self

    def search_players(self, current: str, limit: int) -> list[dict[str, object]]:
        self.current = current
        self.limit = limit
        return [
            {
                "player_name": "Player",
                "player_tag": "#PLAYER",
                "clan_code": "BE1",
                "townhall": 17,
            }
        ]


class PlanCommandTests(unittest.TestCase):
    def test_plan_collects_thinking_and_both_screenshots(self) -> None:
        self.assertEqual(
            [parameter.name for parameter in Planning.planning.parameters],
            ["player", "thinking", "strategy_image", "base_image"],
        )
        self.assertTrue(all(parameter.required for parameter in Planning.planning.parameters))


class PlanEmojiTests(unittest.TestCase):

    def test_level_caps_use_town_hall_data_without_undercutting_current_level(self) -> None:
        with patch.dict(
            TOWN_HALL_MAX_LEVELS,
            {"Test Unit": (0, 3, 5)},
        ):
            self.assertEqual(town_hall_max_level("Test Unit", 1, 1), 1)
            self.assertEqual(town_hall_max_level("Test Unit", 2, 1), 3)
            self.assertEqual(town_hall_max_level("Test Unit", 3, 6), 6)
            self.assertEqual(town_hall_max_level("Test Unit", 4, 2), 2)
        self.assertEqual(town_hall_max_level("Unknown Unit", 18, 3), 3)


    def test_missing_emojis_keep_readable_unit_names(self) -> None:
        base_image = SimpleNamespace(url="https://example.com/base.png")
        strategy_image = SimpleNamespace(url="https://example.com/strategy.png")
        player = {
            "name": "Planner",
            "tag": "#PLAYER",
            "townHallLevel": 18,
            "heroes": [],
            "heroEquipment": [],
            "troops": [{"name": "Ruin Witch", "level": 4, "village": "home"}],
            "spells": [{"name": "Angry Spell", "level": 4, "village": "home"}],
        }

        embeds = build_planning_embeds(
            player,
            "Fallback test.",
            strategy_image,
            base_image,
        )
        army_fields = {
            field.name: field.value
            for field in embeds.pages[2].fields
        }

        self.assertIn(
            "Ruin Witch\u00a0`\u200e 4/",
            army_fields["Dark Elixir Troops"],
        )
        self.assertIn(
            "Angry Spell\u00a0`\u200e 4/",
            army_fields["Dark Spells"],
        )


class PlanNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_interaction_updates_the_selected_page(self) -> None:
        pages = [
            discord.Embed(title="Overview"),
            discord.Embed(title="Hero Kit"),
            discord.Embed(title="Army Kit"),
        ]
        overview_extra = discord.Embed(title="Overview Base")
        view = PlanningView(
            PlanningEmbeds(pages=pages, overview_extras=(overview_extra,)),
        )
        response = SimpleNamespace(
            is_done=lambda: False,
            defer=AsyncMock(side_effect=discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"),
                {"code": 10062, "message": "Unknown interaction"},
            )),
        )
        message = SimpleNamespace(edit=AsyncMock())
        interaction = SimpleNamespace(response=response, message=message)

        for selected_index in (1, 2, 0):
            with self.subTest(page=selected_index):
                response.defer.reset_mock()
                message.edit.reset_mock()

                await view.children[selected_index].callback(interaction)

                response.defer.assert_awaited_once_with()
                expected_embeds = [pages[selected_index]]
                if selected_index == 0:
                    expected_embeds.append(overview_extra)
                message.edit.assert_awaited_once_with(embeds=expected_embeds, view=view)
                self.assertEqual(
                    [item.style for item in view.children],
                    [
                        discord.ButtonStyle.primary
                        if index == selected_index
                        else discord.ButtonStyle.secondary
                        for index in range(len(pages))
                    ],
                )
