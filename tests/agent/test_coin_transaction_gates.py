"""Coin history requires Core access even through SQL aliases and joins."""

from dataclasses import replace
from tempfile import TemporaryDirectory
import unittest

from elbow_helper.configuration import roles
from elbow_helper.agent.access import AgentAccessLost, require_evidence_access
from elbow_helper.agent.datasets.catalogue import SOURCES
from elbow_helper.agent.datasets.query import query_context, run_query
from agent.dataset_helpers import synthetic_datasets
from agent import test_evidence_scope as fixtures


class CoinTransactionGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = fixtures.ScopeTests()
        fixture.setUp()
        self.context = replace(fixture.context, private_pass=True)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paths = self.context.bot.paths = synthetic_datasets(directory.name)

    async def test_core_only_across_query_routes(self):
        for sql in ("SELECT count(*) AS n FROM achievements.coin_transactions",
                    'SELECT * FROM "achievements"."coin_transactions"',
                    "SELECT * FROM coin_transactions",
                    "SELECT a.* FROM achievements.coin_transactions a JOIN achievements.user_coins b ON 1=1"):
            for levels in (set(), {"lead"}, {"lead_plus"}, {"core", "lead"}):
                with self.subTest(sql=sql, levels=levels):
                    result, touched = run_query(SOURCES, self.paths, levels, sql)
                    self.assertEqual("error" not in result, "core" in levels)
                    if "error" not in result:
                        self.assertIn("core", touched)

    async def test_shared_destination_and_revocation(self):
        self.context.member.roles.append(fixtures.Role(next(iter(roles.CORE))))
        result = await query_context(self.context, "SELECT count(*) AS n FROM achievements.coin_transactions")
        self.assertNotIn("error", result)
        self.assertIn("core", self.context.state.required_access)
        self.context.member.roles.pop()
        with self.assertRaises(AgentAccessLost):
            await require_evidence_access(self.context)
        self.context.member.roles.append(fixtures.Role(next(iter(roles.CORE))))
        self.context = replace(self.context, private_pass=False, state=type(self.context.state)())
        self.context.source_message.channel = self.context.guild.channels[1]
        result = await query_context(self.context, "SELECT * FROM achievements.coin_transactions")
        self.assertIn("error", result)
        self.assertTrue(result["available_privately"])
