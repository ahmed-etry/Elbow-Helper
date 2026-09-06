from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import discord

from elbow_helper.features.attack_plans.cog import Planning
from elbow_helper.features.attack_plans.emojis import application_emoji_name
from elbow_helper.features.attack_plans.formatting import PlanningEmbeds
from elbow_helper.features.attack_plans.formatting import build_planning_embeds
from elbow_helper.features.attack_plans.formatting import required_plan_unit_names
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


class PlanAutocompleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_autocomplete_uses_health_player_search(self) -> None:
        harness = _PlanningHarness()

        choices = await Planning.player_autocomplete(harness, None, "Play")

        self.assertEqual(harness.current, "Play")
        self.assertEqual(harness.limit, 25)
        self.assertEqual([(choice.name, choice.value) for choice in choices], [("Player - BE1 - TH17 - #PLAYER", "#PLAYER")])


class PlanEmojiTests(unittest.TestCase):
    def test_required_emoji_names_exclude_super_troops_and_siege_machines(self) -> None:
        unit_names = required_plan_unit_names()
        emoji_names = [application_emoji_name(name) for name in unit_names]

        self.assertEqual(len(unit_names), len(set(unit_names)))
        self.assertEqual(len(emoji_names), len(set(emoji_names)))
        self.assertIn("Ruin Witch", unit_names)
        self.assertIn("Angry Spell", unit_names)
        self.assertIn("Monolith Arrow", unit_names)
        self.assertIn("Revenge Deck", unit_names)
        self.assertNotIn("Super Barbarian", unit_names)
        self.assertNotIn("Sky Wagon", unit_names)
        self.assertEqual(set(unit_names), set(TOWN_HALL_MAX_LEVELS))

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

    def test_clashperk_emoji_names_are_preserved(self) -> None:
        self.assertEqual(application_emoji_name("Barbarian King"), "BarbarianKing")
        self.assertEqual(application_emoji_name("Spiky Ball"), "SpikeyBall")
        self.assertEqual(application_emoji_name("Ruin Witch"), "ruin_witch")
        self.assertEqual(application_emoji_name("Angry Spell"), "angry_spell")
        self.assertEqual(application_emoji_name("Monolith Arrow"), "monolith_arrow")
        self.assertEqual(application_emoji_name("Revenge Deck"), "revenge_deck")

    def test_current_units_render_as_icons_in_their_plan_sections(self) -> None:
        base_image = SimpleNamespace(url="https://example.com/base.png")
        player = {
            "name": "Planner",
            "tag": "#PLAYER",
            "townHallLevel": 18,
            "heroes": [
                {"name": "Archer Queen", "level": 110, "village": "home"},
                {"name": "Dragon Duke", "level": 25, "village": "home"},
            ],
            "pets": [
                {"name": "Greedy Raven", "level": 10},
            ],
            "heroEquipment": [
                {"name": "Monolith Arrow", "level": 27},
                {"name": "Electro Fangs", "level": 18},
                {"name": "Rocket Backpack", "level": 27},
                {"name": "Revenge Deck", "level": 27},
            ],
            "troops": [
                {"name": "Ruin Witch", "level": 4, "village": "home"},
                {"name": "Sky Wagon", "level": 4, "village": "home"},
                {
                    "name": "Super Barbarian",
                    "level": 13,
                    "village": "home",
                    "superTroopIsActive": True,
                },
            ],
            "spells": [
                {"name": "Angry Spell", "level": 4, "village": "home"},
            ],
        }
        icon_names = (
            "Archer Queen",
            "Dragon Duke",
            "Greedy Raven",
            "Monolith Arrow",
            "Electro Fangs",
            "Rocket Backpack",
            "Revenge Deck",
            "Ruin Witch",
            "Angry Spell",
        )
        tokens = {
            name: f"<:{application_emoji_name(name)}:{index}>"
            for index, name in enumerate(icon_names, start=1000)
        }

        embeds = build_planning_embeds(
            player,
            "Use the new units.",
            base_image,
            emoji_tokens=tokens,
        )

        overview = embeds.pages[0]
        overview_fields = {field.name: field.value for field in overview.fields}
        self.assertIn(
            f'{tokens["Archer Queen"]} `\u200e110/',
            overview_fields["Heroes"],
        )
        self.assertIn(tokens["Greedy Raven"], overview_fields["Pets"])

        hero_kit = embeds.pages[1]
        queen_field = next(
            field for field in hero_kit.fields
            if tokens["Archer Queen"] in field.name
        )
        duke_field = next(
            field for field in hero_kit.fields
            if tokens["Dragon Duke"] in field.name
        )
        self.assertIn(tokens["Monolith Arrow"], queen_field.value)
        self.assertNotIn("Monolith Arrow", queen_field.value)
        for name in ("Electro Fangs", "Rocket Backpack", "Revenge Deck"):
            self.assertIn(tokens[name], duke_field.value)
            self.assertNotIn(name, duke_field.value)

        army_kit = embeds.pages[2]
        army_fields = {field.name: field.value for field in army_kit.fields}
        self.assertEqual(
            list(army_fields),
            ["Elixir Troops", "Dark Elixir Troops", "Elixir Spells", "Dark Spells"],
        )
        troop_value = army_fields["Dark Elixir Troops"]
        spell_value = army_fields["Dark Spells"]
        self.assertIn(f'{tokens["Ruin Witch"]} `\u200e 4/', troop_value)
        self.assertIn(f'{tokens["Angry Spell"]} `\u200e 4/', spell_value)
        army_text = " ".join(field.value for field in army_kit.fields)
        self.assertNotIn("Super Barbarian", army_text)
        self.assertNotIn("Sky Wagon", army_text)

        self.assertEqual(overview.title, "Attack Plan: Planner • TH18")
        self.assertEqual(overview.description, "`#PLAYER`")
        self.assertEqual(overview.image.url, base_image.url)
        self.assertIsNone(overview.thumbnail.url)
        self.assertEqual(hero_kit.thumbnail.url, base_image.url)
        self.assertIsNone(hero_kit.image.url)
        self.assertEqual(army_kit.thumbnail.url, base_image.url)
        self.assertIsNone(army_kit.image.url)

    def test_missing_emojis_keep_readable_unit_names(self) -> None:
        base_image = SimpleNamespace(url="https://example.com/base.png")
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
            base_image,
        )
        army_fields = {
            field.name: field.value
            for field in embeds.pages[2].fields
        }

        self.assertIn(
            "Ruin Witch `\u200e 4/",
            army_fields["Dark Elixir Troops"],
        )
        self.assertIn(
            "Angry Spell `\u200e 4/",
            army_fields["Dark Spells"],
        )

    def test_troop_rows_use_four_clashperk_style_level_entries(self) -> None:
        base_image = SimpleNamespace(url="https://example.com/base.png")
        troop_names = ["Barbarian", "Archer", "Giant", "Goblin", "Wall Breaker"]
        tokens = {
            name: f"<:{application_emoji_name(name)}:{index}>"
            for index, name in enumerate(troop_names, start=2000)
        }
        player = {
            "name": "Planner",
            "tag": "#PLAYER",
            "townHallLevel": 10,
            "heroes": [],
            "heroEquipment": [],
            "troops": [
                {"name": name, "level": 5, "village": "home"}
                for name in troop_names
            ],
            "spells": [],
        }

        with patch.dict(
            TOWN_HALL_MAX_LEVELS,
            {"Barbarian": (6,) * 18},
        ):
            embeds = build_planning_embeds(
                player,
                "Layout test.",
                base_image,
                emoji_tokens=tokens,
            )

        army_fields = {
            field.name: field.value
            for field in embeds.pages[2].fields
        }
        lines = army_fields["Elixir Troops"].splitlines()
        self.assertEqual([line.count("\u200e") for line in lines], [4, 1])
        self.assertIn(f'{tokens["Barbarian"]} `\u200e 5/6 \u200f`', lines[0])


class PlanNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_category_buttons_switch_directly_to_the_selected_page(self) -> None:
        pages = [
            discord.Embed(title="Overview"),
            discord.Embed(title="Hero Kit"),
            discord.Embed(title="Army Kit"),
        ]
        view = PlanningView(PlanningEmbeds(pages=pages))
        response_state = {"done": False}
        interaction_events: list[str] = []

        async def defer() -> None:
            interaction_events.append("defer")
            response_state["done"] = True

        async def edit(**kwargs: object) -> None:
            interaction_events.append("edit")

        response = SimpleNamespace(
            is_done=lambda: response_state["done"],
            defer=AsyncMock(side_effect=defer),
        )
        message = SimpleNamespace(edit=AsyncMock(side_effect=edit))
        interaction = SimpleNamespace(response=response, message=message)

        labels = ["Overview", "Hero Kit", "Army Kit"]
        for selected_index, label in enumerate(labels):
            with self.subTest(label=label):
                response_state["done"] = False
                interaction_events.clear()
                response.defer.reset_mock()
                message.edit.reset_mock()
                button = next(item for item in view.children if item.label == label)

                await button.callback(interaction)

                response.defer.assert_awaited_once_with()
                message.edit.assert_awaited_once_with(
                    embed=pages[selected_index],
                    view=view,
                )
                self.assertEqual(interaction_events, ["defer", "edit"])
                self.assertEqual(
                    [item.style for item in view.children],
                    [
                        discord.ButtonStyle.primary
                        if index == selected_index
                        else discord.ButtonStyle.secondary
                        for index in range(len(labels))
                    ],
                )
