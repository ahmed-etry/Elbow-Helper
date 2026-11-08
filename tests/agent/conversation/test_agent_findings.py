"""Synthetic uneven samples and field meanings used by factual answers."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads.clash import read_clash
from elbow_helper.agent.reads.ranked_measures import metrics


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
        with patch("elbow_helper.agent.reads.clash.require_evidence_access", AsyncMock()):
            return await read_clash(context, arguments)

    async def test_raw_latest_log_keeps_paging_without_a_ranked_aggregate(self):
        result = await self.read({"%23P0": {"items": [
            battle(3, 100), battle(2, 80), battle(1, 60),
            battle(3, 100, mode="HOME_VILLAGE"), battle(3, 100, attack=True),
            battle(3, 100, day=9), battle(3, 100, day=15),
        ]}, "%23P2": {"items": [battle(0, 40, mode="LEGEND")]}, "%23P8": None},
            kind="battle_log", tags=["#P0", "#P2", "#P8", "#P0"],
            after="2026-10-10", before="2026-10-15",
            limit=1, offset=1)
        self.assertNotIn('aggregate', result)
        self.assertEqual(result['items'][0]['coverage']['scope'], 'latest_rolling_log')
        self.assertFalse(result['items'][0]['coverage']['history_complete'])
        self.assertEqual(len(result["items"][0]["data"]["items"]), 1)

    def test_missing_metrics_have_separate_denominators_and_empty_is_not_zero(self):
        result = metrics([battle(3, None), battle(None, 80), battle(True, float("nan")),
                          battle(8, 101)])
        self.assertEqual(result["average_stars"]["denominator"], 1)
        self.assertEqual(result["average_destruction_percent"]["denominator"], 1)
        self.assertEqual(result["missing_stars"], 3)
        self.assertIsNone(metrics([])["average_stars"]["value"])

    async def test_unknown_dates_cannot_establish_requested_window_coverage(self):
        unknown = {**battle(3, 100), "battleTimestamp": "unknown"}
        result = await self.read({'%23P0': {'items': [unknown]}}, kind='battle_log',
                                 tags=['#P0'], after='2026-10-10')
        self.assertEqual(result['items'][0]['data']['items'], [])
        self.assertEqual(result['items'][0]['coverage']['unknown_timestamp_rows'], 1)
        self.assertFalse(result['items'][0]['coverage']['selection_complete'])


    async def test_aggregate_rejects_non_battle_requests(self):
        result = await self.read({}, kind="player", tags=["#P0"], aggregate="ranked_attacks")
        self.assertIn("error", result)

    async def test_latest_log_rejects_every_old_aggregate_without_a_live_call(self):
        for aggregate in ('ranked_attacks', 'ranked_defenses', 'all_attacks', 'all_defenses'):
            with self.subTest(aggregate=aggregate):
                result = await self.read({}, kind='battle_log', tags=['#P0'], aggregate=aggregate)
                self.assertIn('error', result)
