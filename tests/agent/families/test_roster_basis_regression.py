"""Roster and activity entry points share the seasonal community population."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.families.identity_reads import TOOLS
from agent.engine.test_agent_plan_flow import _context


class RosterBasisTests(unittest.IsolatedAsyncioTestCase):
    async def test_cwl_only_rosters_and_activity_use_seasonal_members_unless_explicit(self):
        context = _context()
        for role in context.member.roles:
            role.name = 'Access'
            role.is_default = lambda: False
        clan = CLANS['BE4']
        self.assertTrue(clan.cwl_only)

        async def fetch(path, **kwargs):
            if path.endswith('/leaguegroup'):
                data = {'season': '2025-12', 'clans': [
                    {'tag': clan.tag, 'members': [{'tag': '#Q0', 'name': 'Seasonal'}]}]}
            elif path.startswith('/players/'):
                data = {'tag': '#Q0', 'name': 'Seasonal', 'townHallLevel': 17, 'donations': 5}
            else:
                data = {'tag': clan.tag, 'members': 1,
                        'memberList': [{'tag': '#P0', 'name': 'Parked', 'townHallLevel': 3, 'donations': 0}]}
            return SimpleNamespace(ok=True, status=200, payload=data)

        context = replace(context, bot=SimpleNamespace(clash_client=SimpleNamespace(
            configured=True, get=AsyncMock(side_effect=fetch))),
            account_links=SimpleNamespace(get_links_by_tags=lambda tags: {
                tag: {'discord_user_id': 41} for tag in tags}),
            cwl_queries=SimpleNamespace(attendance=lambda **kwargs: {'rows': [], 'coverage': {}}))
        roster_tool = next(tool for tool in TOOLS if tool.definition.name == 'read_clan_rosters')
        cohort_tool = next(tool for tool in TOOLS if tool.definition.name == 'read_member_cohort')
        roster = await roster_tool.handler(context, {'clan_codes': ['BE4']})
        self.assertEqual([row['player_tag'] for row in roster['rows']], ['#Q0'])
        with patch('elbow_helper.agent.families.cohort_activity.searchable_channel_ids',
                   AsyncMock(return_value=())):
            for activity in (False, True):
                cohort = await cohort_tool.handler(context, {'basis': 'clan_rosters',
                    'clan_codes': ['BE4'], 'include_activity': activity,
                    'after': '2025-12-01T00:00:00+00:00'})
                self.assertEqual([account['player_tag'] for row in cohort['rows']
                    for account in row['accounts']], ['#Q0'])
                self.assertEqual(cohort['clans'][0]['basis'], roster['clans'][0]['basis'])
        explicit = await cohort_tool.handler(context, {'basis': 'clan_rosters',
            'clan_codes': ['BE4'], 'roster_basis': 'current_in_game'})
        self.assertEqual([account['player_tag'] for row in explicit['rows']
            for account in row['accounts']], ['#P0'])
