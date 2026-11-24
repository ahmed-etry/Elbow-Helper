"""Filtered retained cohorts must survive composition into war reads."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.reads import identity_reads
from elbow_helper.agent.reads import record_wars, war_tools as war_reads
from tests.agent.request_fixtures import flow_context as _context
from tests.agent.reads.record_stub import with_record


class CohortSelectionRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_accounts_reads_the_accounts_of_a_war_participation_report(self):
        # Live: a month of participation, then read_member_accounts with that step's report as cohort_ref.
        context = with_record(replace(_context(), account_links=SimpleNamespace(
            get_links_for_members=lambda ids: {41: [{'player_tag': '#P0'}, {'player_tag': '#Q0'}]},
            get_links_by_tags=lambda tags: {tag: {'discord_user_id': 41, 'player_tag': tag} for tag in tags})))
        context.guild.get_member = lambda identifier: context.member if identifier == 41 else None
        for role in context.member.roles:
            role.name, role.is_default = 'Core', lambda: False
        source = {'rows': [{'player_tag': '#P0', 'player_name': '#P0', 'war_id': '#C0', 'clan_code': 'BEH',
                            'attacks_expected': 2, 'attacks_used': 1, 'missed': 1}], 'war_ids': ['#C0'], 'coverage': {}}
        self.enterContext(patch.object(record_wars, 'regular_wars', AsyncMock(return_value=source)))
        participation_tool = next(tool for tool in war_reads.TOOLS if tool.definition.name == 'read_war_participation')
        month = await participation_tool.handler(context, {'clan_codes': ['BEH'], 'war_type': 'REG',
            'after': '2026-11-01T00:00:00+00:00', 'before': '2026-12-01T00:00:00+00:00'})
        accounts_tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        profiles = {'#P0': {'status': 'ok', 'data': {'name': 'First', 'townHallLevel': 17}}}
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=profiles)):
            names = await accounts_tool.handler(context, {'cohort_ref': month['report_id']})
        self.assertNotIn('error', names)
        # The war's accounts only, not every account their owner has.
        self.assertEqual([row['player_tag'] for row in names['rows']], ['#P0'])
        # A report of a kind the option does not take says which kinds it takes.
        names_ref = await accounts_tool.handler(context, {'cohort_ref': names['report_id']})
        self.assertNotIn('error', names_ref)
        context.state.reports[month['report_id']] = replace(context.state.reports[month['report_id']],
                                                            family='read_current_war')
        wrong = await accounts_tool.handler(context, {'cohort_ref': month['report_id']})
        self.assertEqual(wrong['cause'], 'invalid_arguments')
        self.assertIn('read_war_participation', wrong['error'])
        self.assertNotIn('no longer available', wrong['error'])

    async def test_filtered_cohort_keeps_only_retained_accounts_after_links_change(self):
        context = _context()
        for role in context.member.roles:
            role.name = 'Core'
            role.is_default = lambda: False
        links = Mock(return_value={41: [{'player_tag': '#P0'}, {'player_tag': '#Q0'}]})
        context = replace(context, account_links=SimpleNamespace(
            get_links_for_members=links, get_links_by_tags=lambda tags: {
                tag: {'discord_user_id': 41, 'player_tag': tag} for tag in tags}))
        profiles = {'#P0': {'status': 'ok', 'data': {'townHallLevel': 17, 'leagueTier': {'id': 9}}},
                    '#Q0': {'status': 'ok', 'data': {'townHallLevel': 16, 'leagueTier': {'id': 1}}}}
        # Keep the actor's access roles while supplying a role-free cohort member.
        actor = SimpleNamespace(id=41, display_name='Synthetic', name='synthetic',
                                nick=None, joined_at=None, roles=[])
        context.guild.members = [actor]
        context.guild.get_member = lambda identifier: context.member if identifier == 41 else None
        cohort_tool = next(tool for tool in identity_reads.TOOLS
                           if tool.definition.name == 'read_member_cohort')
        participation_tool = next(tool for tool in war_reads.TOOLS
                               if tool.definition.name == 'read_war_participation')
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=profiles)):
            cohort = await cohort_tool.handler(context, {'basis': 'joined_discord',
                'town_hall_min': 17, 'ranked_tier_ids': [9]})
        self.assertEqual([row['player_tag'] for row in cohort['rows'][0]['accounts']], ['#P0'])
        links.return_value = {41: [{'player_tag': '#Q0'}, {'player_tag': '#R0'}]}
        source = {'rows': [{'player_tag': tag, 'player_name': tag, 'war_id': '#C0',
                           'clan_code': 'BEH', 'attacks_expected': 2, 'attacks_used': 1, 'missed': 1}
                          for tag in ('#P0', '#Q0', '#R0')], 'war_ids': ['#C0'], 'coverage': {}}
        context = with_record(context)
        self.enterContext(patch.object(record_wars, 'regular_wars', AsyncMock(return_value=source)))
        result = await participation_tool.handler(context, {'cohort_ref': cohort['report_id'],
            'war_type': 'REG', 'season': '2026-09'})
        self.assertEqual([row['player_tag'] for row in result['rows']], ['#P0'])
        self.assertEqual(links.call_count, 1)

        # A member-only snapshot may resolve links; an explicitly empty account
        # selection must stay empty even when new links become available.
        member_only = await cohort_tool.handler(context, {'basis': 'joined_discord'})
        expanded = await participation_tool.handler(context, {'cohort_ref': member_only['report_id'],
            'war_type': 'REG', 'season': '2026-09'})
        self.assertEqual([row['player_tag'] for row in expanded['rows']], ['#Q0', '#R0'])
        self.assertEqual(links.call_count, 2)
        links.return_value = {41: []}
        empty = await cohort_tool.handler(context, {'basis': 'joined_discord', 'include_accounts': True})
        self.assertFalse(any(account.get('player_tag') for account in empty['rows'][0]['accounts']))
        links.return_value = {41: [{'player_tag': '#Q0'}]}
        unexpanded = await participation_tool.handler(context, {'cohort_ref': empty['report_id'],
            'war_type': 'REG', 'season': '2026-09'})
        self.assertEqual(unexpanded['cause'], 'empty_cohort')
        self.assertEqual(links.call_count, 3)
