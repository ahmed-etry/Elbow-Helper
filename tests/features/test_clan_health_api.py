from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.features.clan_health.api import ClanHealthCollector
from elbow_helper.features.clan_health.config import UTC


class PlayerLevelCollectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Synthetic GET /players response: pets share troops' level/maxLevel/village
        # fields, with no isPet or isTroop discriminator in the raw API payload.
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "clan_health_player.json"
        self.profile = json.loads(fixture.read_text(encoding="utf-8"))

    async def _collect(self, profile: dict) -> dict:
        repository = MagicMock()
        repository.latest_snapshots.return_value = {}
        collector = ClanHealthCollector(MagicMock(), repository, MagicMock())

        async def fetch(path, **_kwargs):
            if path == f"/players/{encode_clash_tag(profile['tag'])}":
                return profile
            if path.endswith("/capitalraidseasons?limit=12"):
                return {"items": []}
            return {
                "members": 1,
                "memberList": [{"tag": profile["tag"], "name": profile["name"]}],
            }

        with (
            patch.object(collector, "_fetch_coc_json", side_effect=fetch),
            patch.object(
                collector, "_fetch_cwl_wars_for_clan",
                new=AsyncMock(return_value=([], [])),
            ),
        ):
            result, warnings = await collector.collect_clan_live(
                clan_code="BEH",
                cycle_start=datetime(2026, 10, 12, tzinfo=UTC),
                cycle_end=datetime(2026, 10, 20, tzinfo=UTC),
            )

        self.assertEqual(warnings, [])
        self.assertEqual(len(result["players"]), 1)
        return result["players"][0]

    async def test_profile_separates_all_current_pets_from_home_troops(self) -> None:
        row = await self._collect(self.profile)

        self.assertEqual(row["pet_sum"], 106)
        self.assertEqual(row["troop_sum"], 42)
        self.assertEqual(row["hero_sum"], 90)
        self.assertEqual(row["equipment_sum"], 18)
        self.assertEqual(row["spell_sum"], 17)

    async def test_each_pet_level_change_affects_only_the_pet_total(self) -> None:
        # Independent of the production name set, in Pet House unlock order.
        names = (
            "L.A.S.S.I", "Electro Owl", "Mighty Yak", "Unicorn", "Frosty",
            "Diggy", "Poison Lizard", "Phoenix", "Spirit Fox", "Angry Jelly",
            "Sneezy", "Greedy Raven",
        )
        for name in names:
            with self.subTest(pet=name):
                profile = deepcopy(self.profile)
                pet = next(troop for troop in profile["troops"] if troop["name"] == name)
                pet["level"] -= 1
                row = await self._collect(profile)

                self.assertEqual(row["pet_sum"], 105)
                self.assertEqual(row["troop_sum"], 42)

    async def test_pet_name_normalization_accepts_punctuation_case_and_spacing(self) -> None:
        for name in ("LASSI", " l.a.s.s.i. ", "L.a.s.s.i"):
            with self.subTest(name=name):
                profile = deepcopy(self.profile)
                pet = next(troop for troop in profile["troops"] if troop["name"] == "L.A.S.S.I")
                pet["name"] = name
                row = await self._collect(profile)

                self.assertEqual(row["pet_sum"], 106)
                self.assertEqual(row["troop_sum"], 42)
