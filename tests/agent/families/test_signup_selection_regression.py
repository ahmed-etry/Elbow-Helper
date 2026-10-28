"""Current roster selection through retained cohorts and the SQLite owner facade."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.families.core import family_tool
from elbow_helper.agent.families.war_reads import TOOLS
from elbow_helper.features.rosters.config import CWL_SIGNUP_ROSTER_ID
from elbow_helper.features.rosters.repository import RosterRepository
from elbow_helper.features.rosters.services.queries import RosterQueries
from agent.engine.test_agent_plan_flow import _context


class SignupSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repository = RosterRepository(Path(directory.name) / 'rosters.db')
        self.context = replace(_context(), roster_queries=RosterQueries(repository))
        self.tool = next(tool for tool in TOOLS if tool.definition.name == 'read_cwl_results')
        for name, role in [('Pool', None), ('Assigned', CLANS['BEM'].cwl_role_id)]:
            roster = repository.create_roster(guild_id=self.context.guild.id, name=name,
                clan_code='BEM', role_id=role, max_members=50)
            if name == 'Pool':
                self.assertEqual(roster.id, CWL_SIGNUP_ROSTER_ID)
            roster, _ = repository.start_cycle(roster.id, '2026-09')
            repository.add_members(roster.id, roster.active_cycle_id, 41,
                [{'player_tag': tag, 'player_name': tag, 'townhall': 17} for tag in ('#P0', '#Q0', '#Y0')], 50)
            repository.add_post(roster.id, 91, 100 + roster.id)

    async def test_filters_and_top_n_preserve_roster_population(self):
        for metric in ('signup_pool', 'assigned_rosters'):
            with self.subTest(metric=metric):
                result = await self.tool.handler(self.context, {'metric': metric,
                    'account_tags': ['#Q0', '#Y0'], 'top_n': 1})
                self.assertEqual([row['player_tag'] for row in result['rows']], ['#Q0'])
                self.assertEqual(result['roster_population'], 3)
                self.assertEqual(result['selected_population'], 2)
                self.assertEqual(result['rosters'][0]['accounts'], 3)

    async def test_retained_cohort_selects_accounts_and_invalid_reference_is_rejected(self):
        async def source(context, arguments):
            return {'rows': [{'member_id': 41, 'account_tags': ['#Y0']}]}
        cohort = await family_tool('read_member_cohort', 'Read selected accounts.', {}, source).handler(self.context, {})
        for metric in ('signup_pool', 'assigned_rosters'):
            result = await self.tool.handler(self.context, {'metric': metric, 'cohort_ref': cohort['report_id']})
            self.assertEqual([row['player_tag'] for row in result['rows']], ['#Y0'])
            invalid = await self.tool.handler(self.context, {'metric': metric, 'cohort_ref': 'missing'})
            self.assertEqual(invalid['cause'], 'report_unavailable')

    async def test_historical_selectors_are_rejected_before_current_rosters_are_queried(self):
        selectors = {'season': '2000-01', 'season_keys': ['2000-01'],
            'after': '2000-01-01T00:00:00+00:00', 'before': '2000-02-01T00:00:00+00:00',
            'war_ids': ['W1'], 'latest_completed': 1, 'history_limit': 1}
        with patch.object(self.context.roster_queries, 'list_for_guild', AsyncMock()) as query:
            for metric in ('signup_pool', 'assigned_rosters'):
                for key, value in selectors.items():
                    result = await self.tool.handler(self.context, {'metric': metric, key: value})
                    self.assertEqual(result['cause'], 'invalid_arguments')
            query.assert_not_awaited()
