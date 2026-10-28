"""Filtered retained cohorts must survive composition into war reads."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.families import identity_reads, war_reads
from agent.engine.test_agent_plan_flow import _context


class CohortSelectionRegressionTests(unittest.IsolatedAsyncioTestCase):
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
        attendance_tool = next(tool for tool in war_reads.TOOLS
                               if tool.definition.name == 'read_war_attendance')
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=profiles)):
            cohort = await cohort_tool.handler(context, {'basis': 'joined_discord',
                'town_hall_min': 17, 'ranked_tier_ids': [9]})
        self.assertEqual([row['player_tag'] for row in cohort['rows'][0]['accounts']], ['#P0'])
        links.return_value = {41: [{'player_tag': '#Q0'}, {'player_tag': '#R0'}]}
        source = {'rows': [{'player_tag': tag, 'player_name': tag, 'war_id': '#C0',
                           'clan_code': 'BEH', 'attacks_expected': 2, 'attacks_used': 1, 'missed': 1}
                          for tag in ('#P0', '#Q0', '#R0')], 'war_ids': ['#C0'], 'coverage': {}}
        context = replace(context, cwl_queries=SimpleNamespace(attendance=lambda **selection: source))
        result = await attendance_tool.handler(context, {'cohort_ref': cohort['report_id'],
            'war_type': 'REG', 'season': '2026-09'})
        self.assertEqual([row['player_tag'] for row in result['rows']], ['#P0'])
        self.assertEqual(links.call_count, 1)

        # A member-only snapshot may resolve links; an explicitly empty account
        # selection must stay empty even when new links become available.
        member_only = await cohort_tool.handler(context, {'basis': 'joined_discord'})
        expanded = await attendance_tool.handler(context, {'cohort_ref': member_only['report_id'],
            'war_type': 'REG', 'season': '2026-09'})
        self.assertEqual([row['player_tag'] for row in expanded['rows']], ['#Q0', '#R0'])
        self.assertEqual(links.call_count, 2)
        links.return_value = {41: []}
        empty = await cohort_tool.handler(context, {'basis': 'joined_discord', 'include_accounts': True})
        self.assertFalse(any(account.get('player_tag') for account in empty['rows'][0]['accounts']))
        links.return_value = {41: [{'player_tag': '#Q0'}]}
        unexpanded = await attendance_tool.handler(context, {'cohort_ref': empty['report_id'],
            'war_type': 'REG', 'season': '2026-09'})
        self.assertEqual(unexpanded['cause'], 'empty_cohort')
        self.assertEqual(links.call_count, 3)
