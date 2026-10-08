"""Entry eligibility does not grant evidence access."""

from types import SimpleNamespace
import unittest

from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID, CORE, LEAD
from elbow_helper.features.agent.access import has_agent_entry_access


class AgentEntryTests(unittest.TestCase):
    def test_entry_role_groups(self):
        for role_id, allowed in ((AGENT_TESTER_ROLE_ID, True),
                                 (next(iter(CORE)), True),
                                 (next(iter(LEAD)), False), (1, False)):
            with self.subTest(role_id=role_id):
                member = SimpleNamespace(roles=[SimpleNamespace(id=role_id)])
                self.assertEqual(has_agent_entry_access(member), allowed)
        self.assertFalse(has_agent_entry_access(SimpleNamespace(roles=[])))
