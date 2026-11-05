from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from elbow_helper.configuration.clans import CLAN_TAGS
from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.features.clan_health.api import ClanHealthCollector
from elbow_helper.features.clan_health.config import UTC


class LivePlayerRowTagTests(unittest.IsolatedAsyncioTestCase):
    WINDOW = {
        "season_key": "2026-10",
        "cycle_start": datetime(2026, 10, 12, tzinfo=UTC),
        "cycle_end": datetime(2026, 10, 20, tzinfo=UTC),
    }

    def _collector(self) -> ClanHealthCollector:
        client = MagicMock()
        client.configured = True
        return ClanHealthCollector(client, MagicMock(), MagicMock())

    async def test_player_is_found_whatever_the_written_form_of_the_tag(self) -> None:
        for written in ("#P0Y2L", "p0y2l", " #p0y2l ", "#POY2L"):
            with self.subTest(written=written):
                collector = self._collector()
                paths = []

                async def fetch(path, **_kwargs):
                    paths.append(path)
                    return {"clan": {"tag": CLAN_TAGS["BEH"]}}

                entry = {"players": [
                    {"player_tag": "#Q2L", "player_name": "Other"},
                    {"player_tag": "#P0Y2L", "player_name": "Wanted"},
                ]}
                with patch.object(collector, "_fetch_coc_json", side_effect=fetch), \
                        patch.object(collector, "collect_clan_live", return_value=(entry, [])):
                    row, warnings = await collector.try_live_player_row(player_tag=written, **self.WINDOW)

                self.assertEqual(paths, [f"/players/{encode_clash_tag('#P0Y2L')}"])
                self.assertEqual(warnings, [])
                self.assertEqual(row["player_name"], "Wanted")

    async def test_an_invalid_tag_is_never_sent_to_clash(self) -> None:
        collector = self._collector()
        with patch.object(collector, "_fetch_coc_json") as fetched:
            row, warnings = await collector.try_live_player_row(player_tag="#ZZZ", **self.WINDOW)

        fetched.assert_not_called()
        self.assertIsNone(row)
        self.assertEqual(warnings, ["Current player details couldn't be loaded."])


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

        # League wars come from the family record; the live collection asks Clash for none.
        with patch.object(collector, "_fetch_coc_json", side_effect=fetch) as fetched:
            result, warnings = await collector.collect_clan_live(
                clan_code="BEH",
                cycle_start=datetime(2026, 10, 12, tzinfo=UTC),
                cycle_end=datetime(2026, 10, 20, tzinfo=UTC),
            )

        self.assertEqual(warnings, [])
        self.assertEqual(len(result["players"]), 1)
        self.assertFalse(any("league" in call.args[0] for call in fetched.call_args_list))
        self.assertNotIn("war_cwl_wars_in_window", result["metrics"])
        return result["players"][0]

    async def test_stored_tags_are_canonical_and_invalid_raid_tags_are_skipped(self) -> None:
        repository = MagicMock()
        repository.latest_snapshots.return_value = {}
        collector = ClanHealthCollector(MagicMock(), repository, MagicMock())
        raid_member = {"attacks": 1, "attackLimit": 5, "capitalResourcesLooted": 10}

        async def fetch(path, **_kwargs):
            if path.startswith("/players/"):
                return None
            if path.endswith("/capitalraidseasons?limit=12"):
                return {"items": [{"endTime": "20261019T070000.000Z", "members": [
                    {**raid_member, "tag": " #p0y2l ", "name": "Raider"},
                    {**raid_member, "tag": "#ZZZ", "name": "Broken"},
                ]}]}
            return {"members": 1, "memberList": [{"tag": "p0y2l", "name": "Raider"}]}

        with patch.object(collector, "_fetch_coc_json", side_effect=fetch):
            result, _warnings = await collector.collect_clan_live(
                clan_code="BEH",
                cycle_start=datetime(2026, 10, 12, tzinfo=UTC),
                cycle_end=datetime(2026, 10, 20, tzinfo=UTC),
            )

        self.assertEqual([row["player_tag"] for row in result["players"]], ["#P0Y2L"])
        stored = repository.store_raid_activity.call_args.args[0]
        self.assertEqual([row["player_tag"] for row in stored], ["#P0Y2L"])

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
