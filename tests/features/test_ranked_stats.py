from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.ranked_stats.cog import RankedStats
from elbow_helper.features.ranked_stats.queries import RankedQueries
from elbow_helper.features.ranked_stats.store import RankedStore


def stamp(day=1, minute=0):
    return (datetime(2026, 9, day, tzinfo=timezone.utc) + timedelta(minutes=minute)).isoformat(timespec="microseconds")


def observation(tag="#P0", *, wins=10, day=1, minute=0, trophies=100,
                member=42, clan="#Q0", status="ok", season=None):
    fields = {"trophies": trophies}
    if wins is not None:
        fields["attackWins"] = wins
    if season:
        fields["legendStatistics"] = {"currentSeason": {"id": season}}
    return {"player_tag": tag, "observed_at": stamp(day, minute), "member_id": member,
            "clan_tag": clan, "player_name": "Synthetic account", "status": status, "fields": fields}


class RankedWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ranked.sqlite3"
        self.store = RankedStore(self.path)
        self.store.initialize()
        self.queries = RankedQueries(self.store)

    def read(self, selector="member_ids", values=(42,), **arguments):
        return self.queries.window(after=stamp(), before=stamp(4), selector=selector, values=values, **arguments)

    def test_persisted_daily_deltas_do_not_become_attack_usage_or_quotas(self):
        self.store.append([observation(wins=10), observation(wins=13, minute=5),
                           observation(wins=14, day=2), observation(wins=16, day=2, minute=5)])
        # A new store instance sees the observations after a restart.
        result = RankedQueries(RankedStore(self.path)).window(
            after=stamp(), before=stamp(4), selector="member_ids", values=(42,))
        row = result["accounts"][0]
        self.assertEqual(row["attack_wins_observed"], 6)
        self.assertEqual([day["attack_wins_observed"] for day in row["daily"]], [3, 2])
        self.assertIsNone(row["attacks_used"])
        self.assertIsNone(row["attacks_available"])
        self.assertFalse(result["coverage_complete"])

    def test_resets_and_missing_counter_samples_break_intervals(self):
        self.store.append([observation(wins=20), observation(wins=2, minute=5),
                           observation(wins=4, minute=10), observation(wins=None, minute=15),
                           observation(wins=8, minute=20), observation(wins=10, minute=25)])
        row = self.read()["accounts"][0]
        self.assertEqual(row["counter_resets"], 1)
        self.assertEqual(row["attack_wins_observed"], 4)

    def test_failed_requests_never_count_as_zero_and_mark_stale_ranking(self):
        self.store.append([observation(wins=20), observation(wins=None, minute=5, status="unavailable"),
                           observation(wins=25, minute=10), observation(wins=None, minute=15, status="unavailable")])
        row = self.read()["accounts"][0]
        self.assertIsNone(row["attack_wins_observed"])
        self.assertEqual(row["failed_samples"], 2)
        self.assertEqual(row["status"], "stale")
        self.assertEqual(row["ranking_observed_at"], stamp(minute=10))

    def test_daily_attribution_omits_intervals_crossing_utc_midnight(self):
        self.store.append([observation(wins=10, minute=1439), observation(wins=15, day=2, minute=4)])
        row = self.read()["accounts"][0]
        self.assertEqual(row["attack_wins_observed"], 5)
        self.assertTrue(all(day["attack_wins_observed"] is None for day in row["daily"]))

    def test_season_change_rejects_a_delta_even_if_new_total_is_higher(self):
        self.store.append([observation(wins=10, season="old"), observation(wins=20, minute=5, season="new")])
        row = self.read()["accounts"][0]
        self.assertEqual(row["counter_resets"], 1)
        self.assertIsNone(row["attack_wins_observed"])

    def test_clan_and_owner_filters_do_not_bridge_time_outside_the_selection(self):
        for selector, middle in (("clan_tags", {"clan": "#Q2"}), ("member_ids", {"member": 43})):
            with self.subTest(selector=selector):
                tag = "#P0" if selector == "clan_tags" else "#P2"
                self.store.append([observation(tag, wins=10), observation(tag, wins=20, minute=5, **middle),
                                   observation(tag, wins=30, minute=10)])
                values = ("#Q0",) if selector == "clan_tags" else (42,)
                result = self.read(selector, values)
                row = next(row for row in result["accounts"] if row["player_tag"] == tag)
                self.assertIsNone(row["attack_wins_observed"])

    def test_ranks_ties_paging_unobserved_accounts_and_half_open_window(self):
        self.store.append([observation("#P0", trophies=200), observation("#P2", trophies=200),
                           observation("#P8", trophies=100), observation("#P9", trophies=900, day=4)])
        first = self.read("account_tags", ("#P0", "#P2", "#P8", "#P9"), limit=2)
        self.assertEqual([row["selection_rank"] for row in first["accounts"]], [1, 1])
        self.assertEqual(first["next_offset"], 2)
        self.assertEqual(first["unobserved_selections"], ["#P9"])
        second = self.read(limit=2, offset=2)
        self.assertEqual(second["accounts"][0]["selection_rank"], 3)

    def test_row_limit_can_stop_before_the_selected_ownership_observation(self):
        import json
        row = observation(member=43)
        row["fields_json"] = json.dumps(row.pop("fields"))
        with patch.object(self.store, "read_window", return_value=([row], True)):
            result = self.read()
        self.assertEqual(result["accounts"], [])
        self.assertFalse(result["complete"])
        self.assertIn("snapshot_row_limit_reached", result["limitations"])


class RankedCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = SimpleNamespace(configured=True, get=AsyncMock())
        links = SimpleNamespace(get_all_links=lambda: {
            "#P0": {"discord_user_id": 42}, "#P2": {"discord_user_id": 43}})
        self.bot = SimpleNamespace(clash_client=self.client, paths=SimpleNamespace(data_root=Path(self.temp.name)))
        self.cog = RankedStats(self.bot, links)
        self.cog.store.initialize()
        clock = patch("elbow_helper.features.ranked_stats.cog.datetime")
        self.addCleanup(clock.stop)
        clock.start().now.return_value = datetime(2026, 9, 1, tzinfo=timezone.utc)

    async def test_all_linked_accounts_use_the_owned_client_and_partial_fields_are_logged(self):
        self.client.get.return_value = SimpleNamespace(ok=True, status=200,
            payload={"trophies": 100, "attackWinsRenamed": 20,
                     "leagueTier": {"id": 1, "name": "Synthetic league"},
                     "legendStatistics": {"currentSeason": {"rank": 1, "trophies": 100, "id": "sample"}}})
        with self.assertLogs("elbow_helper.features.ranked_stats.cog", level="WARNING") as logs:
            await self.cog.collect_once()
        self.assertTrue(any("attackWins" in line for line in logs.output))
        self.assertEqual({call.args[0] for call in self.client.get.await_args_list}, {"/players/%23P0", "/players/%23P2"})
        result = self.cog.queries.window(after="2026-01-01T00:00:00.000000+00:00",
            before="2027-01-01T00:00:00.000000+00:00", selector="member_ids", values=(42, 43))
        self.assertEqual(len(result["accounts"]), 2)
        for row in result["accounts"]:
            self.assertNotIn("attackWins", row["ranking"])
            self.assertEqual(row["ranking"]["leagueTier"]["name"], "Synthetic league")
            self.assertIsNone(row["attack_wins_observed"])

    async def test_one_failed_account_does_not_stop_other_snapshots(self):
        self.client.get.side_effect = [RuntimeError("synthetic failure"),
            SimpleNamespace(ok=True, status=200, payload={"trophies": 100, "attackWins": 0})]
        with self.assertLogs("elbow_helper.features.ranked_stats.cog"):
            await self.cog.collect_once()
        rows, truncated = self.cog.store.read_window(after="2000", before="2100", selector="member_ids", values=(42, 43))
        self.assertFalse(truncated)
        self.assertEqual(sorted(row["status"] for row in rows), ["ok", "unavailable"])

    async def test_missing_key_skips_collection_without_fabricating_observations(self):
        self.client.configured = False
        await self.cog.collect_once()
        self.client.get.assert_not_awaited()

    async def test_loop_starts_after_initialization_and_cancels_on_unload(self):
        with patch("elbow_helper.features.ranked_stats.cog.start_resilient_loop") as start:
            await self.cog.cog_load()
        start.assert_called_once_with(self.cog.snapshot_loop)
        with patch.object(self.cog.snapshot_loop, "cancel") as cancel:
            self.cog.cog_unload()
        cancel.assert_called_once()
