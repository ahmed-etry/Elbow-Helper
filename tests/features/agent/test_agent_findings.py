"""Synthetic uneven samples and field meanings used by factual answers."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.datasets.clash import read_clash
from elbow_helper.features.agent.datasets.findings import battle_aggregate, metrics
from elbow_helper.features.agent.research.history import search_date


def battle(stars, destruction, *, mode="RANKED", attack=False, day=12):
    return {"battleType": mode, "attack": attack, "stars": stars,
            "destructionPercentage": destruction,
            "battleTimestamp": f"2026-10-{day:02}T12:00:00Z"}


class FindingTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, payloads, **arguments):
        async def get(path, **kwargs):
            payload = payloads[path.split("/")[-2 if path.endswith("battlelog") else -1]]
            return SimpleNamespace(ok=payload is not None, payload=payload, status=404)

        context = SimpleNamespace(bot=SimpleNamespace(clash_client=SimpleNamespace(
            configured=True, get=AsyncMock(side_effect=get))), deadline_monotonic=None)
        with patch("elbow_helper.features.agent.datasets.clash.require_evidence_access", AsyncMock()):
            return await read_clash(context, arguments)

    async def test_weighted_cohort_uses_all_matching_rows_not_drilldown_page(self):
        result = await self.read({"%23P0": {"items": [
            battle(3, 100), battle(2, 80), battle(1, 60),
            battle(3, 100, mode="HOME_VILLAGE"), battle(3, 100, attack=True),
            battle(3, 100, day=9), battle(3, 100, day=15),
        ]}, "%23P2": {"items": [battle(0, 40, mode="LEGEND")]}, "%23P8": None},
            kind="battle_log", tags=["#P0", "#P2", "#P8", "#P0"],
            aggregate="ranked_defenses", after="2026-10-10", before="2026-10-15",
            limit=1, offset=1)
        cohort = result["aggregate"]
        self.assertEqual(cohort["battles"], 4)
        self.assertEqual(cohort["average_stars"], {
            "numerator": 6, "denominator": 4, "value": 1.5, "display": "1.5000"})
        self.assertEqual(cohort["triple_rate_percent"]["value"], 25)
        self.assertEqual(cohort["average_destruction_percent"]["value"], 70)
        self.assertEqual(cohort["coverage"]["requested_accounts"], 3)
        self.assertEqual(cohort["coverage"]["unavailable_tags"], ["#P8"])
        self.assertFalse(cohort["coverage"]["history_complete"])
        self.assertEqual(result["items"][0]["aggregate"]["battles"], 3)
        self.assertEqual(len(result["items"][0]["data"]["items"]), 1)

    def test_missing_metrics_have_separate_denominators_and_empty_is_not_zero(self):
        result = metrics([battle(3, None), battle(None, 80), battle(True, float("nan")),
                          battle(8, 101)])
        self.assertEqual(result["average_stars"]["denominator"], 1)
        self.assertEqual(result["average_destruction_percent"]["denominator"], 1)
        self.assertEqual(result["missing_stars"], 3)
        self.assertIsNone(metrics([])["average_stars"]["value"])

    def test_unknown_dates_cannot_establish_requested_window_coverage(self):
        unknown = {**battle(3, 100), "battleTimestamp": "unknown"}
        result = battle_aggregate({"items": [unknown]}, "ranked_defenses",
                                  after=search_date("2026-10-10"))
        self.assertEqual(result["battles"], 0)
        self.assertEqual(result["coverage"]["unknown_timestamp_rows"], 1)
        self.assertFalse(result["coverage"]["selection_complete"])

    async def test_meanings_survive_full_and_summary_views(self):
        for detail in ("summary", "full"):
            player = await self.read({"%23P0": {"attackWins": 0, "trophies": 0}},
                                     kind="player", tags=["#P0"], detail=detail)
            self.assertIn("war_participation", player["field_meanings"]["attackWins"]["does_not_establish"])
            self.assertIn("cwl_participation", player["field_meanings"]["clan"]["does_not_establish"])
            self.assertIn("cwl_eligibility", player["field_meanings"]["heroes"]["does_not_establish"])
            clan = await self.read({"%23P0": {"members": 48, "memberList": [{"clanRank": 48}]}},
                                   kind="clan", tags=["#P0"], detail=detail)
            self.assertIn("hard_cap", clan["field_meanings"]["members"]["does_not_establish"])
            self.assertIn("join_time", clan["field_meanings"]["memberList[].clanRank"]["does_not_establish"])
            self.assertIn("eligibility_standard", clan["field_meanings"]["warLeague"]["does_not_establish"])

    async def test_aggregate_rejects_non_battle_requests(self):
        result = await self.read({}, kind="player", tags=["#P0"], aggregate="ranked_attacks")
        self.assertIn("error", result)
