"""A refused dataset read records what the asker could see privately, so Show full answer is offered."""
from dataclasses import replace
import unittest
from types import SimpleNamespace

from elbow_helper.agent.engine.models import EvidenceScope
from elbow_helper.agent.reads.sql.query import query_context
from elbow_helper.configuration.roles import CORE, LEAD
from tests.agent.files.test_agent_spreadsheets import _context
from tests.agent.reads.dataset_helpers import synthetic_datasets
from tests.temp_directory import TemporaryDirectory

LEAD_ONLY = "SELECT count(*) AS n FROM achievements.user_coins"


class DatasetScopeFieldTests(unittest.IsolatedAsyncioTestCase):
    async def read_in_public_room(self, role_id):
        directory = self.enterContext(TemporaryDirectory())
        context = replace(_context(directory), private_pass=False)
        context.bot.paths = synthetic_datasets(directory, only={"achievements"})
        context.member.roles = [SimpleNamespace(id=role_id)]
        context.state.scope = EvidenceScope(100, frozenset(), frozenset())
        return context, await query_context(context, LEAD_ONLY)

    async def test_lead_only_dataset_asked_by_a_lead_in_a_public_room_is_offered_privately(self):
        context, result = await self.read_in_public_room(next(iter(LEAD)))
        self.assertEqual(result["cause"], "access_denied")
        self.assertTrue(result["available_privately"])
        self.assertEqual(context.state.scope_audit.private_candidates, {"dataset:lead"})
        self.assertEqual(context.state.scope_audit.excluded, {"dataset:lead": "role_scope"})

    async def test_asker_without_lead_access_gets_no_private_offer(self):
        context, result = await self.read_in_public_room(next(iter(CORE - LEAD)))
        self.assertEqual(result["cause"], "access_denied")
        self.assertNotIn("available_privately", result)
        self.assertFalse(context.state.scope_audit.private_candidates)


if __name__ == "__main__":
    unittest.main()
