"""Retain public owner context without inventing account purpose or access."""

from types import SimpleNamespace
import unittest

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import CO_APPLICANT_ROLE_ID, SLEEPING_CO_ROLE_ID
from elbow_helper.agent.safety.access import has_access_requirements
from elbow_helper.agent.reads.domain_facts import clan_profile
from elbow_helper.agent.reads.member_context import owner_context


class RecommendationContextTests(unittest.IsolatedAsyncioTestCase):


    def test_sleeping_leadership_context_does_not_grant_workflow_access(self):
        member = SimpleNamespace(id=12, display_name='Synthetic sleeping captain',
                                 roles=[SimpleNamespace(id=SLEEPING_CO_ROLE_ID, name='Sleeping Co')])
        self.assertEqual(owner_context(member, 12)['leadership']['status'], 'sleeping')
        guild = SimpleNamespace(get_member=lambda _: member)
        self.assertFalse(has_access_requirements(guild, 12, {'recruitment'}))


    def test_configured_clan_is_not_proof_of_operational_status(self):
        for code in CLANS:
            self.assertIsNone(clan_profile(code)['operational_status'])

    def test_leadership_applicant_is_not_an_active_leader(self):
        member = SimpleNamespace(id=12, display_name='Synthetic applicant',
            roles=[SimpleNamespace(id=CO_APPLICANT_ROLE_ID, name='Co-Leader Applicant')])
        self.assertEqual(owner_context(member, 12)['leadership']['status'], 'applicant')
