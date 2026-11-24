"""Complete join-window evidence survives the end of active trials."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import MEMBER_ROLE_ID, TRIAL_ROLE_ID
from elbow_helper.agent.reads import identity_reads
from tests.agent.request_fixtures import flow_context as _context


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
