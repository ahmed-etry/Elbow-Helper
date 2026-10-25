"""Raw inventories cannot bypass the inventory command's Lead restriction."""

from tempfile import TemporaryDirectory
import unittest

from elbow_helper.features.agent.datasets.catalogue import SOURCES
from elbow_helper.features.agent.datasets.query import run_query
from features.agent.dataset_helpers import synthetic_datasets


class InventoryDatasetGateTests(unittest.TestCase):
    def test_raw_inventory_requires_lead_and_earned_achievements_stay_public(self):
        with TemporaryDirectory() as directory:
            paths = synthetic_datasets(directory)
            for table in ("user_coins", "user_stats", "raffle_tickets"):
                for levels in (set(), {"lead_plus"}, {"core"}, {"lead"}):
                    with self.subTest(table=table, levels=levels):
                        result, touched = run_query(SOURCES, paths, levels,
                                                    f"SELECT count(*) AS n FROM achievements.{table}")
                        self.assertEqual("error" not in result, "lead" in levels)
                        self.assertEqual(touched, {"lead"} if "lead" in levels else set())
            result, touched = run_query(SOURCES, paths, set(),
                                        "SELECT count(*) AS n FROM achievements.user_achievements")
            self.assertNotIn("error", result)
            self.assertFalse(touched)
