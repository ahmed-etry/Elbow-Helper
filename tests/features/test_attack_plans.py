from __future__ import annotations

import unittest

from elbow_helper.features.attack_plans.cog import Planning


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
