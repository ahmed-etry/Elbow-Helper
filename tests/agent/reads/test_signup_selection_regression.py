"""Roster selection by league month through retained cohorts and the SQLite owner facade."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tests.temp_directory import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.reads.war_tools import TOOLS
from elbow_helper.features.rosters.config import CWL_SIGNUP_ROSTER_ID
from elbow_helper.features.rosters.repository import RosterRepository
from elbow_helper.features.rosters.services.queries import RosterQueries
from tests.agent.request_fixtures import flow_context as _context


def _at(day):
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()


class SignupSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repository = RosterRepository(Path(directory.name) / 'rosters.db')
        self.context = replace(_context(), roster_queries=RosterQueries(repository))
        self.tool = next(tool for tool in TOOLS if tool.definition.name == 'read_cwl_results')
        # Cycles are named after the month signups open; each serves the next month's league.
        cycles = (('2026-08', '2026-08-18', ('#A0', '#B0')), ('2026-09', '2026-09-18', ('#P0', '#Q0', '#Y0')))
        for name, role in [('Pool', None), ('Assigned', CLANS['BEM'].cwl_role_id)]:
            roster = repository.create_roster(guild_id=self.context.guild.id, name=name,
                clan_code='BEM', role_id=role, max_members=50)
            if name == 'Pool':
                self.assertEqual(roster.id, CWL_SIGNUP_ROSTER_ID)
            for key, opened, tags in cycles:
                with patch('elbow_helper.features.rosters.repository.repository.time.time',
                           return_value=_at(opened)):
                    roster, _ = repository.start_cycle(roster.id, key)
                repository.add_members(roster.id, roster.active_cycle_id, 41,
                    [{'player_tag': tag, 'player_name': tag, 'townhall': 17} for tag in tags], 50)
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
                self.assertEqual(result['rosters'][0]['season'], '2026-10')

    async def test_war_selectors_are_rejected_before_rosters_are_queried(self):
        selectors = {'after': '2000-01-01T00:00:00+00:00', 'before': '2000-02-01T00:00:00+00:00',
            'war_ids': ['W1'], 'latest_completed': 1, 'history_limit': 1}
        with patch.object(self.context.roster_queries, 'list_for_guild', AsyncMock()) as query:
            for metric in ('signup_pool', 'assigned_rosters'):
                for key, value in selectors.items():
                    result = await self.tool.handler(self.context, {'metric': metric, key: value})
                    self.assertEqual(result['cause'], 'invalid_arguments')
            query.assert_not_awaited()

    async def test_league_month_reads_the_cycle_opened_the_month_before(self):
        # "Who signed up for the October CWL" reads the cycle opened in September, and an
        # earlier league reads its own kept cycle.
        for metric in ('signup_pool', 'assigned_rosters'):
            for selection, tags in (({'season': '2026-10'}, ['#P0', '#Q0', '#Y0']),
                                    ({'season': '2026-09'}, ['#A0', '#B0']),
                                    ({'season_keys': ['2026-09']}, ['#A0', '#B0'])):
                with self.subTest(metric=metric, selection=selection):
                    result = await self.tool.handler(self.context, {'metric': metric, **selection})
                    self.assertNotIn('error', result)
                    self.assertEqual([row['player_tag'] for row in result['rows']], tags)
                    self.assertEqual([roster['season'] for roster in result['rosters']],
                                     [selection.get('season') or selection['season_keys'][0]])
                    self.assertEqual({row['cycle_id'] for row in result['rows']},
                                     {result['rosters'][0]['cycle_id']})

    async def test_league_month_without_a_kept_cycle_is_empty(self):
        for metric in ('signup_pool', 'assigned_rosters'):
            for season in ('2026-08', '2026-11'):
                with self.subTest(metric=metric, season=season):
                    result = await self.tool.handler(self.context, {'metric': metric, 'season': season})
                    self.assertNotIn('error', result)
                    self.assertEqual(result['rows'], [])
                    self.assertEqual([source['cause'] for source in result['missing_sources']],
                                     ['missing_history'])

    async def test_league_month_with_a_war_selector_is_still_refused(self):
        result = await self.tool.handler(self.context, {'metric': 'assigned_rosters',
            'season': '2026-10', 'latest_completed': 1})
        self.assertEqual(result['cause'], 'invalid_arguments')
        self.assertEqual(result['unsupported_selectors'], ['latest_completed'])
