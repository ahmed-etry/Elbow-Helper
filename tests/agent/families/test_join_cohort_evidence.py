"""Complete join-window evidence survives the end of active trials."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import MEMBER_ROLE_ID, TRIAL_ROLE_ID
from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.families import identity_reads
from elbow_helper.agent.families.model_view import family_model_view
from agent.engine.test_agent_plan_flow import _context


class JoinCohortEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def context(self, links):
        def member(identifier, day, roles):
            return SimpleNamespace(id=identifier, display_name=str(identifier),
                joined_at=datetime(2026, 9, day, tzinfo=timezone.utc),
                roles=[SimpleNamespace(id=value, name=str(value), is_default=lambda: False) for value in roles])
        members = [member(1, 1, [MEMBER_ROLE_ID, CLANS['BE1'].member_role_id]),
                   member(2, 28, [TRIAL_ROLE_ID, CLANS['BES'].member_role_id, CLANS['BEH'].member_role_id]),
                   member(3, 15, [MEMBER_ROLE_ID, CLANS['BEM'].member_role_id]),
                   member(4, 19, [CLANS['BEW'].member_role_id])]
        context = replace(_context(), account_links=links)
        context.guild.members = members
        context.guild.get_member = lambda identifier: (context.member if identifier == context.member.id else
            next((m for m in members if m.id == identifier), None))
        return context

    async def test_all_dates_roles_and_link_presence_reach_writer_without_optional_flags(self):
        links = SimpleNamespace(get_links_for_members=lambda _: {1: [{'player_tag': '#P0'}],
                                                                2: [{'player_tag': '#Q0'}]})
        context = self.context(links)
        profiles = {tag: {'status': 'ok', 'data': {'name': tag,
                    'clan': {'tag': CLANS[code].tag, 'name': CLANS[code].name}}}
                    for tag, code in (('#P0', 'BE1'), ('#Q0', 'BEH'))}
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=profiles)):
            result = await identity_reads.member_cohort(context, {'basis': 'joined_discord',
                'joined_after': '2026-09-01T00:00:00Z', 'joined_before': '2026-10-01T00:00:00Z'})
        visible = answer_evidence({'joins': family_model_view({**result, 'family': 'read_member_cohort'})})['joins']
        rows = {row['member_id']: row for row in visible['rows']}
        self.assertEqual(set(rows), {1, 2, 3, 4})
        self.assertTrue(rows[1]['family_membership_roles']['member']['held'])
        self.assertTrue(rows[2]['family_membership_roles']['trial']['held'])
        self.assertEqual(len(rows[2]['home_clans']), 2)
        self.assertEqual(rows[2]['accounts'][0]['clan']['tag'], CLANS['BEH'].tag)
        self.assertTrue(rows[2]['has_elbow_helper_links'])
        self.assertFalse(rows[3]['has_elbow_helper_links'])
        self.assertFalse(rows[4]['family_membership_roles']['member']['held'])
        self.assertFalse(rows[4]['family_membership_roles']['trial']['held'])
        self.assertEqual(rows[4]['home_clans'], [])
        self.assertEqual(visible['selection_window']['joined_before'], '2026-10-01T00:00:00Z')

    async def test_unavailable_links_and_live_profiles_remain_unknown(self):
        context = self.context(None)
        result = await identity_reads.member_cohort(context, {'basis': 'joined_discord'})
        self.assertTrue(all(row['has_elbow_helper_links'] is None for row in result['rows']))
        self.assertFalse(result['count_complete'])
        context = self.context(SimpleNamespace(get_links_for_members=lambda _: {1: [{'player_tag': '#P0'}]}))
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value={'#P0': {'status': 'unavailable'}})):
            result = await identity_reads.member_cohort(context, {'basis': 'joined_discord'})
        row = next(row for row in result['rows'] if row['member_id'] == 1)
        self.assertTrue(row['has_elbow_helper_links'])
        self.assertIsNone(row['accounts'][0]['clan'])
        self.assertNotIn('accepted_at', row)
