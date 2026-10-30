"""Member and account profile facts reach the agent only when the question needs them."""

from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.families import identity_reads
from elbow_helper.agent.families.model_view import family_model_view
from elbow_helper.agent.plan.results import model_result, model_view
from elbow_helper.configuration.clans import CLANS
from agent.families.test_named_account_stats import lookup_context, named_member

MEMBER_PROFILE = ('nickname', 'joined_at', 'family_membership_roles', 'home_clans',
                  'participation_clans', 'home_clan_status', 'home_clan_basis')
ACCOUNT_PROFILE = ('role', 'ranked_league', 'legacy_league', 'trophies', 'donations',
                   'war_stars', 'observed_at')
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

    async def test_default_view_keeps_identity_clan_town_hall_and_status_only(self):
        _, page = await self.lookup({})
        view = family_model_view(page)
        self.assertEqual(len(view['rows']), 10)
        for row in view['rows']:
            for field in KEPT:
                self.assertIn(field, row)
            for field in ACCOUNT_PROFILE:
                self.assertNotIn(field, row)
        member = view['members'][0]
        self.assertEqual((member['member_id'], member['member_name']), (42, 'Synthetic Pilot'))
        self.assertEqual(member['account_tags'], [f'#A{index}' for index in range(10)])
        for field in MEMBER_PROFILE:
            self.assertNotIn(field, member)
        self.assertNotIn('home_clan_basis', json.dumps(view))

    async def test_include_profile_returns_the_facts_without_the_basis_text(self):
        _, page = await self.lookup({'include_profile': True})
        view = family_model_view(page)
        for row in view['rows']:
            for field in ACCOUNT_PROFILE:
                self.assertIn(field, row)
        self.assertEqual(view['rows'][0]['trophies'], 4000)
        self.assertEqual(view['rows'][0]['ranked_league']['name'], 'Synthetic League A')
        member = view['members'][0]
        for field in MEMBER_PROFILE[:-1]:
            self.assertIn(field, member)
        self.assertEqual(member['nickname'], 'Synthetic Nick')
        self.assertEqual(member['home_clan_status'], 'resolved')
        self.assertEqual([clan['clan_code'] for clan in member['home_clans']], ['BES'])
        self.assertNotIn('home_clan_basis', member)

    async def test_retained_report_keeps_every_fact_whatever_the_option(self):
        context, page = await self.lookup({})
        retained = json.loads(context.state.reports[page['report_id']].payload)
        for row in retained['rows']:
            for field in ACCOUNT_PROFILE:
                self.assertIn(field, row)
        member = retained['summary']['members'][0]
        for field in MEMBER_PROFILE:
            self.assertIn(field, member)
        self.assertIn('home_clan_basis', page['members'][0])

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

    async def test_unknown_accounts_follow_the_same_default(self):
        context = lookup_context([owner()], [('#A0', 'Synthetic account 0', 42)])
        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value={
                '#A0': {'tag': '#A0', 'status': 'unavailable'}})):
            page = await tool.handler(context, {'member_ids': [42]})
        view = family_model_view(page)
        self.assertEqual(view['rows'][0]['profile_status'], 'unavailable')
        self.assertTrue(view['unknown_accounts'])
        for row in view['unknown_accounts']:
            for field in ACCOUNT_PROFILE:
                self.assertNotIn(field, row)

    async def test_cohort_selection_from_a_default_lookup_still_sees_every_fact(self):
        context, page = await self.lookup({})
        from elbow_helper.agent.families.projections import cohort_selection
        self.assertEqual(await cohort_selection(context, page['report_id'], 'account_tags'),
                         {'account_tags': [f'#A{index}' for index in range(10)]})
        retained = json.loads(context.state.reports[page['report_id']].payload)['rows']
        self.assertTrue(all(row['ranked_league'] and row['donations'] for row in retained))

    async def test_default_view_is_smaller_for_ten_accounts(self):
        def size(page):
            return len(json.dumps(model_view(model_result(page)), ensure_ascii=False, separators=(',', ':')))
        _, plain = await self.lookup({})
        _, full = await self.lookup({'include_profile': True})
        self.assertLess(size(plain), size(full) * 0.6)

    def test_option_is_off_by_default_and_described(self):
        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        option = tool.definition.parameters['properties']['include_profile']
        self.assertEqual((option['type'], option['default']), ('boolean', False))
        for topic in ('trial or member status', 'join date', 'home clans', 'leagues', 'trophies',
                      'donations', 'career totals'):
            self.assertIn(topic, option['description'])


if __name__ == '__main__':
    unittest.main()
