"""Unknown policy stays unknown and hierarchy semantics never grant access."""
from types import SimpleNamespace
import unittest

from elbow_helper.configuration.clans import CLAN_ORDER
from elbow_helper.configuration.roles import CORE, LEAD
from elbow_helper.agent.access import has_access_requirements
from elbow_helper.agent.families.domain_facts import clan_profile, domain_reference
from elbow_helper.agent.families.reference_reads import community_reference
from agent.engine.test_agent_plan_flow import _context


class DomainFactTests(unittest.IsolatedAsyncioTestCase):
    def test_encoded_narrative_facts_retain_their_approved_source_locations(self):
        bes = clan_profile('BES')
        self.assertEqual(bes['sources']['town_hall_min']['source'], 'K/clan-profiles.md')
        self.assertEqual(bes['sources']['town_hall_min']['line'], 53)
        self.assertEqual(bes['sources']['purpose']['visibility'], 'public')
        self.assertEqual(clan_profile('BEC')['sources']['purpose']['line'], 79)
        self.assertIsNone(clan_profile('BEC')['town_hall_min'])
        self.assertIsNone(clan_profile('BEP')['operational_cwl_only'])

    async def test_all_nine_clans_keep_unsourced_ranges_and_placeholders_unknown(self):
        result = await community_reference(_context(),{'reference_kind':'clans'})
        self.assertEqual([row['clan_code'] for row in result['rows']],list(CLAN_ORDER))
        for row in result['rows']:
            self.assertIsNone(row['town_hall_max'])
            if row['clan_code']!='BES':
                self.assertIsNone(row['town_hall_min'])
            if row['clan_code']!='BE4':
                self.assertIsNone(row['has_placeholders'])
        self.assertEqual(clan_profile('BES')['town_hall_min'],14)
        self.assertTrue(clan_profile('BE4')['has_placeholders'])
        self.assertFalse(clan_profile('BEP')['configured_cwl_only'])
        self.assertIsNone(clan_profile('BEP')['operational_cwl_only'])

    async def test_owner_hierarchy_does_not_expand_actual_role_access(self):
        core_only = next(iter(CORE-LEAD))
        member = SimpleNamespace(id=41,roles=[SimpleNamespace(id=core_only)])
        guild = SimpleNamespace(get_member=lambda identifier:member)
        result = domain_reference('roles')
        self.assertEqual(result['rows'][0]['holds'],['Lead','Elder','Member'])
        self.assertEqual(result['rows'][1]['lead_plus_alternatives'],['Lead','Co Applicant only'])
        self.assertFalse(result['semantic_facts_grant_permissions'])
        self.assertTrue(has_access_requirements(guild,41,{'core'}))
        self.assertFalse(has_access_requirements(guild,41,{'lead'}))

    async def test_workflow_descriptions_distinguish_events_from_later_decisions(self):
        facts = {row['event']:row for row in domain_reference('workflows')['rows']}
        self.assertIn('finalization',facts['trial_ended_followup_sent']['does_not_establish'])
        self.assertIn('promotion decision',facts['examiner_routed']['does_not_establish'])
        self.assertIn('observed inactivity',facts['hibernating']['does_not_establish'])
