from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from elbow_helper.features.attack_plans.cog import Planning
from elbow_helper.features.attack_plans.emojis import application_emoji_name
from elbow_helper.features.attack_plans.formatting import build_planning_embeds
from elbow_helper.features.attack_plans.formatting import required_plan_unit_names
from elbow_helper.features.attack_plans.unit_levels import TOWN_HALL_MAX_LEVELS
from elbow_helper.features.attack_plans.unit_levels import town_hall_max_level


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
        interaction = SimpleNamespace(user=SimpleNamespace(mention="<@123>"))
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
            interaction,
            player,
            "Use the new units.",
            base_image,
            emoji_tokens=tokens,
        )

        self.assertEqual(embeds.army_sections(), ["troops", "spells"])
        overview = embeds.static_pages[0]
        self.assertIn(
            f'{tokens["Archer Queen"]} `\u200e110/',
            overview.fields[2].value,
        )
        self.assertIn(tokens["Greedy Raven"], overview.fields[3].value)

        hero_kit = embeds.static_pages[1]
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

        troop_value = embeds.army_embeds["troops"].fields[1].value
        spell_value = embeds.army_embeds["spells"].fields[1].value
        self.assertIn(f'{tokens["Ruin Witch"]} `\u200e 4/', troop_value)
        self.assertIn(f'{tokens["Angry Spell"]} `\u200e 4/', spell_value)
        army_text = " ".join(
            field.value
            for embed in embeds.army_embeds.values()
            for field in embed.fields
        )
        self.assertNotIn("Super Barbarian", army_text)
        self.assertNotIn("Sky Wagon", army_text)

    def test_missing_emojis_keep_readable_unit_names(self) -> None:
        interaction = SimpleNamespace(user=SimpleNamespace(mention="<@123>"))
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
            interaction,
            player,
            "Fallback test.",
            base_image,
        )

        self.assertIn(
            "Ruin Witch `\u200e 4/",
            embeds.army_embeds["troops"].fields[1].value,
        )
        self.assertIn(
            "Angry Spell `\u200e 4/",
            embeds.army_embeds["spells"].fields[1].value,
        )

    def test_troop_rows_use_four_clashperk_style_level_entries(self) -> None:
        interaction = SimpleNamespace(user=SimpleNamespace(mention="<@123>"))
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
                interaction,
                player,
                "Layout test.",
                base_image,
                emoji_tokens=tokens,
            )

        lines = embeds.army_embeds["troops"].fields[0].value.splitlines()
        self.assertEqual([line.count("\u200e") for line in lines], [4, 1])
        self.assertIn(f'{tokens["Barbarian"]} `\u200e 5/6 \u200f`', lines[0])
