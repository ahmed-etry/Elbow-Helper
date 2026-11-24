"""Member and account profile facts reach the agent only when the question needs them."""

from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads import identity_reads
from elbow_helper.configuration.clans import CLANS
from tests.agent.reads.test_named_account_stats import lookup_context, named_member

MEMBER_PROFILE = ('nickname', 'joined_at', 'family_membership_roles', 'home_clans',
                  'participation_clans', 'home_clan_status', 'home_clan_basis')
ACCOUNT_PROFILE = ('role',)
ACCOUNT_STANDING = ('ranked_league', 'legacy_league', 'trophies', 'donations', 'war_stars')
ACCOUNT_FACTS = ACCOUNT_PROFILE + ACCOUNT_STANDING + ('observed_at',)
KEPT = ('member_id', 'member_name', 'player_tag', 'player_name', 'clan', 'townHallLevel', 'profile_status')


def owner():
    member = named_member(42, 'Synthetic Pilot', nick='Synthetic Nick')
    member.joined_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    member.roles = [SimpleNamespace(id=CLANS['BES'].member_role_id, name='Synthetic home',
                                    is_default=lambda: False)]
    return member


def profiles(count):
    return {f'#A{index}': {'status': 'ok', 'observed_at': '2026-01-02T00:00:00+00:00', 'data': {
        'name': f'Synthetic account {index}', 'townHallLevel': 12 + index % 5, 'role': 'member',
        'leagueTier': {'id': 105000000 + index % 3, 'name': 'Synthetic League ' + 'ABC'[index % 3],
                       'iconUrls': {'small': 'https://synthetic.invalid/league.png'}},
        'league': {'id': 29000000, 'name': 'Synthetic legacy'},
        'clan': {'tag': CLANS['BES'].tag, 'name': 'Synthetic clan'},
        'trophies': 4000 + index, 'donations': 100 + index, 'warStars': 1000 + index}}
        for index in range(count)}


class ProfileOnRequestTests(unittest.IsolatedAsyncioTestCase):
    async def lookup(self, arguments, count=10):
        context = lookup_context([owner()], [(f'#A{index}', f'Synthetic account {index}', 42)
                                             for index in range(count)])
        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=profiles(count))):
            page = await tool.handler(context, {'member_ids': [42], **arguments})
        return context, page

    async def test_cohort_selection_from_a_default_lookup_still_sees_every_fact(self):
        context, page = await self.lookup({})
        from elbow_helper.agent.reads.projections import cohort_selection
        self.assertEqual(await cohort_selection(context, page['report_id'], 'account_tags'),
                         {'account_tags': [f'#A{index}' for index in range(10)]})
        retained = json.loads(context.state.reports[page['report_id']].payload)['rows']
        self.assertTrue(all(row['ranked_league'] and row['donations'] for row in retained))


if __name__ == '__main__':
    unittest.main()
