"""Each raw dataset keeps its feature's access policy through SQL routes."""

import unittest
from elbow_helper.infrastructure.persistence.documents import write_document

from elbow_helper.agent.tool_support.sql.catalogue import SOURCES
from elbow_helper.agent.tool_support.adapters.role_connections.data import SOURCES as ROLE_SOURCES
from elbow_helper.agent.tool_support.sql.query import run_query
from agent.dataset_helpers import synthetic_datasets
from tests.temp_directory import TemporaryDirectory


class SQLAccessTests(unittest.TestCase):
    def test_dataset_policies_and_public_earned_achievements(self):
        directory = self.enterContext(TemporaryDirectory())
        paths = synthetic_datasets(directory, only={"achievements", "state"})
        queries = [
            (SOURCES, f"SELECT count(*) AS n FROM achievements.{table}", "lead")
            for table in ("user_coins", "user_stats", "raffle_tickets")
        ]
        queries.extend((SOURCES, sql, "core") for sql in (
            "SELECT count(*) AS n FROM achievements.coin_transactions",
            'SELECT * FROM "achievements"."coin_transactions"',
            "SELECT * FROM coin_transactions",
            "SELECT a.* FROM achievements.coin_transactions a JOIN achievements.user_coins b ON 1=1",
        ))
        write_document(ROLE_SOURCES[0].resolve(paths), {"connections": []})
        queries.append((ROLE_SOURCES, "SELECT * FROM state.role_connections", "lead"))
        for sources, sql, required in queries:
            required_levels = {required}
            if " JOIN " in sql:
                required_levels.add("lead")
            for levels in (set(), {"lead_plus"}, {"core"}, {"lead"}, {"core", "lead"}):
                with self.subTest(sql=sql, levels=levels):
                    result, touched = run_query(sources, paths, levels, sql)
                    self.assertEqual("error" not in result, required_levels <= levels)
                    self.assertEqual(touched, required_levels if required_levels <= levels else set())
        result, touched = run_query(SOURCES, paths, set(),
            "SELECT count(*) AS n FROM achievements.user_achievements")
        self.assertNotIn("error", result)
        self.assertFalse(touched)
