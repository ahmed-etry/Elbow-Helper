"""Roster and activity entry points share the seasonal community population."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

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

        activity_record = {'status': 'observed', 'last_active_day': '2025-12-15',
                           'active_streak_days': 4, 'lifetime_messages': 123}
        stored_activity = Mock(return_value={41: activity_record})
        history = AsyncMock(side_effect=AssertionError('No channel history allowed'))
        context = replace(context, bot=SimpleNamespace(clash_client=SimpleNamespace(
            configured=True, get=AsyncMock(side_effect=fetch))),
            achievement_queries=SimpleNamespace(member_activity=stored_activity),
            message_search=SimpleNamespace(history_page=history),
            account_links=SimpleNamespace(get_links_by_tags=lambda tags: {
                tag: {'discord_user_id': 41} for tag in tags}),
            cwl_queries=SimpleNamespace(attendance=lambda **kwargs: {'rows': [], 'coverage': {}}))
        roster_tool = next(tool for tool in TOOLS if tool.definition.name == 'read_clan_rosters')
        cohort_tool = next(tool for tool in TOOLS if tool.definition.name == 'read_member_cohort')
        roster = await roster_tool.handler(context, {'clan_codes': ['BE4']})
        self.assertEqual([row['player_tag'] for row in roster['rows']], ['#Q0'])
        for activity in (False, True):
            cohort = await cohort_tool.handler(context, {'basis': 'clan_rosters',
                'clan_codes': ['BE4'], 'include_activity': activity,
                'after': '2025-12-01T00:00:00+00:00'})
            self.assertEqual([account['player_tag'] for row in cohort['rows']
                for account in row['accounts']], ['#Q0'])
            self.assertEqual(cohort['clans'][0]['basis'], roster['clans'][0]['basis'])
            if activity:
                stored_activity.assert_called_once_with([41])
                self.assertEqual(cohort['rows'][0]['discord_activity'], activity_record)
            else:
                stored_activity.assert_not_called()
        history.assert_not_awaited()
        explicit = await cohort_tool.handler(context, {'basis': 'clan_rosters',
            'clan_codes': ['BE4'], 'roster_basis': 'current_in_game'})
        self.assertEqual([account['player_tag'] for row in explicit['rows']
            for account in row['accounts']], ['#P0'])
