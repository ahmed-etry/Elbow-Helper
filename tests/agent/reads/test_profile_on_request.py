"""Member and account profile facts reach the agent only when the question needs them."""

from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads import identity_reads
from elbow_helper.agent.reads.evidence import family_model_view
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

    async def test_only_requested_profile_and_standing_facts_reach_the_writer(self):
        for profile, standing in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(profile=profile, standing=standing):
                _, page = await self.lookup({'include_profile': profile, 'include_standing': standing})
                view = family_model_view(page)
                self.assertEqual(len(view['rows']), 10)
                wanted = set(KEPT)
                if profile:
                    wanted.update(ACCOUNT_PROFILE)
                if standing:
                    wanted.update(ACCOUNT_STANDING)
                if profile or standing:
                    wanted.add('observed_at')
                for row in view['rows']:
                    for field in set(KEPT) | set(ACCOUNT_FACTS) | {'observed_at'}:
                        self.assertEqual(field in row, field in wanted, field)
                member = view['members'][0]
                self.assertEqual((member['member_id'], member['member_name']), (42, 'Synthetic Pilot'))
                self.assertEqual(member['account_tags'], [f'#A{index}' for index in range(10)])
                for field in MEMBER_PROFILE[:-1]:
                    self.assertEqual(field in member, profile, field)
                self.assertNotIn('home_clan_basis', member)
                if profile:
                    self.assertEqual(member['nickname'], 'Synthetic Nick')
                    self.assertEqual(member['home_clan_status'], 'resolved')
                    self.assertEqual([clan['clan_code'] for clan in member['home_clans']], ['BES'])
                if standing:
                    self.assertEqual(view['rows'][0]['trophies'], 4000)
                    self.assertEqual(view['rows'][0]['ranked_league']['name'], 'Synthetic League A')


    async def test_filters_use_the_full_data_without_the_option(self):
        _, page = await self.lookup({'town_hall_min': 15, 'ranked_tier_prefix': 'Synthetic League B'})
        view = family_model_view(page)
        self.assertEqual([row['player_tag'] for row in view['rows']],
                         [row['player_tag'] for row in page['rows']])
        self.assertTrue(view['rows'])
        for row in view['rows']:
            self.assertGreaterEqual(row['townHallLevel'], 15)
            self.assertNotIn('ranked_league', row)
        for row in page['rows']:
            self.assertTrue(row['ranked_league']['name'].startswith('Synthetic League B'))
        self.assertLess(len(view['rows']), 10)


    async def test_cohort_selection_from_a_default_lookup_still_sees_every_fact(self):
        context, page = await self.lookup({})
        from elbow_helper.agent.reads.projections import cohort_selection
        self.assertEqual(await cohort_selection(context, page['report_id'], 'account_tags'),
                         {'account_tags': [f'#A{index}' for index in range(10)]})
        retained = json.loads(context.state.reports[page['report_id']].payload)['rows']
        self.assertTrue(all(row['ranked_league'] and row['donations'] for row in retained))


if __name__ == '__main__':
    unittest.main()
