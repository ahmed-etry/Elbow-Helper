"""A planner mistake reaches the planner as an error; an account in every checked lineup's absence is a zero."""
from dataclasses import replace
from datetime import datetime
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.families import account_activity, identity_reads, war_reads
from elbow_helper.agent.families.war_reads import TOOLS
from elbow_helper.features.cwl.config import CWL_CLAN_CODES
from agent.engine.test_agent_plan_flow import _context
from agent.families.test_named_account_stats import lookup_context, named_member
from tests.features.cwl_record_stub import LeagueRecord, league_war, member

NON_CWL = next(code for code in CLANS if code not in CWL_CLAN_CODES)


class ConflictingOptionsTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return replace(_context(), cwl_queries=SimpleNamespace())

    async def test_options_every_clan_would_refuse_alike_are_one_error(self):
        codes = list(CWL_CLAN_CODES[:3])
        for metric in ('ass', 'bonus'):
            with self.subTest(metric=metric), patch.object(war_reads, 'stored_read', AsyncMock()) as stored:
                result = await war_reads.cwl_results(self.context(), {'metric': metric, 'clan_codes': codes,
                    'season': '2025-04', 'latest_completed': 3})
                self.assertEqual(result['cause'], 'invalid_arguments')
                self.assertIn('Choose one', result['error'])
                self.assertNotIn('missing_sources', result)
                stored.assert_not_awaited()

    async def test_a_window_with_a_season_is_refused_before_any_clan_is_read(self):
        result = await war_reads.cwl_results(self.context(), {'metric': 'ass', 'clan_codes': list(CWL_CLAN_CODES[:2]),
            'season': '2025-04', 'after': '2025-04-01T00:00:00+00:00', 'before': '2025-05-01T00:00:00+00:00'})
        self.assertEqual(result['cause'], 'invalid_arguments')

    async def test_a_badly_formed_season_refused_by_every_clan_is_one_error(self):
        result = await war_reads.cwl_results(self.context(), {'metric': 'bonus',
            'clan_codes': list(CWL_CLAN_CODES[:2]), 'season': 'not-a-season'})
        self.assertEqual(result['cause'], 'invalid_arguments')
        self.assertNotIn('missing_sources', result)

    async def test_gaps_that_are_not_argument_mistakes_stay_per_clan(self):
        codes = list(CWL_CLAN_CODES[:2])
        unavailable = {'error': 'Stored war evidence is unavailable.', 'cause': 'source_unavailable'}
        with patch.object(war_reads, 'selected_wars', AsyncMock(return_value=unavailable)):
            result = await war_reads.cwl_results(self.context(), {'metric': 'ass', 'clan_codes': codes,
                'season': '2025-04'})
        self.assertNotIn('error', result)
        self.assertEqual([gap['clan_code'] for gap in result['missing_sources']], codes)
        self.assertEqual(result['status'], 'partial')

    async def test_a_clan_outside_cwl_is_an_error_naming_it(self):
        for metric, codes in (('ass', [NON_CWL]), ('bonus', [CWL_CLAN_CODES[0], NON_CWL]),
                              ('performance', [NON_CWL]), ('mega_ass', [NON_CWL])):
            with self.subTest(metric=metric):
                result = await war_reads.cwl_results(self.context(), {'metric': metric, 'clan_codes': codes})
                self.assertEqual(result['cause'], 'invalid_arguments')
                self.assertIn(NON_CWL, result['error'])
                self.assertEqual(result['invalid_fields'], ['clan_codes'])

    async def test_cwl_clans_are_not_refused(self):
        result = await war_reads.cwl_results(replace(_context(), cwl_queries=None),
            {'metric': 'ass', 'clan_codes': [CWL_CLAN_CODES[0]]})
        self.assertEqual(result['cause'], 'missing_setup')


class HistoryWindowTests(unittest.IsolatedAsyncioTestCase):
    async def lookup(self, history):
        context = lookup_context([named_member(42, 'Synthetic Pilot')], [('#A0', 'Synthetic account', 42)])
        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        profile = {'#A0': {'status': 'ok', 'data': {'name': 'Synthetic account'}}}
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=profile)), \
                patch.object(account_activity, 'account_activity', AsyncMock(return_value=history)):
            return await tool.handler(context, {'member_ids': [42], 'after': '2025-11-02T00:00:00+00:00'})

    async def test_a_refused_history_window_reaches_the_planner_as_an_error(self):
        result = await self.lookup({
            'regular_war': {'read_ran': True, 'status': 'complete'},
            'ranked': {'read_ran': False, 'status': 'invalid_selection', 'cause': 'invalid_arguments',
                       'error': 'Use ISO dates with the end after the start and a valid page.'}})
        self.assertEqual(result['cause'], 'invalid_arguments')
        self.assertIn('ISO dates', result['error'])
        self.assertNotIn('history_reads', result)

    async def test_other_history_outcomes_stay_nested(self):
        for status, cause in (('failed', 'source_unavailable'), ('invalid_selection', 'empty_cohort')):
            with self.subTest(status=status):
                result = await self.lookup({'ranked': {'read_ran': status != 'invalid_selection',
                                                       'status': status, 'cause': cause, 'error': 'Synthetic.'}})
                self.assertNotIn('error', result)
                self.assertEqual(result['history_reads']['ranked']['status'], status)


class ReportOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_report_of_another_read_names_the_read_that_owns_it(self):
        context = lookup_context([named_member(42, 'Synthetic Pilot')], [('#A0', 'Synthetic account', 42)])
        lookup = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        attendance = next(tool for tool in TOOLS if tool.definition.name == 'read_war_attendance')
        profile = {'#A0': {'status': 'ok', 'data': {'name': 'Synthetic account'}}}
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=profile)):
            page = await lookup.handler(context, {'member_ids': [42]})
        result = await attendance.handler(context, {'report_id': page['report_id']})
        self.assertEqual(result['cause'], 'invalid_arguments')
        self.assertIn('read_member_accounts', result['error'])
        self.assertEqual(result['invalid_fields'], ['report_id'])
        again = await lookup.handler(context, {'report_id': page['report_id']})
        self.assertEqual(again['report_id'], page['report_id'])

    async def test_an_unknown_report_is_still_unavailable(self):
        context = lookup_context()
        attendance = next(tool for tool in TOOLS if tool.definition.name == 'read_war_attendance')
        result = await attendance.handler(context, {'report_id': 'missing'})
        self.assertEqual(result['cause'], 'report_unavailable')


class KnownZeroTests(unittest.IsolatedAsyncioTestCase):
    END = int(datetime.fromisoformat('2026-09-05T00:00:00+00:00').timestamp())

    async def database(self, rosters):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        record = await LeagueRecord(directory.name).initialize()
        await record.week('BEH', '2026-09', [
            league_war(war_id, index + 1, self.END + index * 10,
                       [member('#P0', name='Account', townhall=17)] if war_id in rosters else [],
                       opponent=('#Q0', '#Q2')[index], team_size=15)
            for index, war_id in enumerate(('#C0', '#C2'))], league='Master League I')
        links = {'#P0': {'discord_user_id': 41}, '#Y0': {'discord_user_id': 77}}
        return replace(_context(), cwl_queries=record.queries(),
            account_links=SimpleNamespace(
                get_links_by_tags=lambda tags: {tag: links.get(tag) for tag in tags},
                get_links_for_members=lambda ids: {i: [{'player_tag': '#Y0'}] for i in ids if i == 77}))

    async def read(self, context, **arguments):
        tool = next(tool for tool in TOOLS if tool.definition.name == 'read_war_attendance')
        return await tool.handler(context, {'war_type': 'CWL', 'clan_codes': ['BEH'], **arguments})

    def absent(self, result):
        return next(row for row in result['rows'] if row['player_tag'] == '#Y0' or row['member_id'] == 77)

    async def test_an_account_in_no_lineup_is_a_known_zero_when_every_war_has_one(self):
        result = await self.read(await self.database({'#C0', '#C2'}), latest_completed=2,
                                 account_tags=['#P0', '#Y0'])
        self.assertEqual(result['wars_without_rosters'], [])
        row = self.absent(result)
        self.assertEqual((row['attacks_used'], row['attacks_expected'], row['missed'], row['war_ids']),
                         (0, 0, 0, []))
        self.assertEqual(row['unconfirmed_opportunities'], 0)
        self.assertTrue(row['totals_complete'])
        self.assertEqual(row['observation_status'], 'not_in_checked_lineups')
        self.assertEqual(row['participation_status'], 'not_in_checked_lineups')
        present = next(row for row in result['rows'] if row['player_tag'] == '#P0')
        self.assertEqual(present['participation_status'], 'warring')

    async def test_a_member_whose_accounts_are_in_no_lineup_is_a_known_zero(self):
        result = await self.read(await self.database({'#C0', '#C2'}), latest_completed=2, member_ids=[77],
                                 unit='member')
        row = self.absent(result)
        self.assertEqual((row['attacks_used'], row['missed']), (0, 0))
        self.assertTrue(row['totals_complete'])
        self.assertEqual(row['participation_status'], 'not_in_checked_lineups')

    async def test_a_war_without_a_lineup_leaves_the_account_unknown(self):
        result = await self.read(await self.database({'#C0'}), latest_completed=2, account_tags=['#P0', '#Y0'])
        self.assertEqual(result['wars_without_rosters'], ['CWL:#C2'])
        row = self.absent(result)
        self.assertIsNone(row['attacks_used'])
        self.assertIsNone(row['missed'])
        self.assertFalse(row['totals_complete'])
        self.assertEqual(row['observation_status'], 'no_stored_participation')
        self.assertEqual(row['participation_status'], 'unknown')

    async def test_a_selection_that_checked_no_war_stays_unknown(self):
        result = await self.read(await self.database({'#C0', '#C2'}), season='2020-01', account_tags=['#Y0'])
        self.assertEqual(result['war_ids'], [])
        row = self.absent(result)
        self.assertIsNone(row['attacks_used'])
        self.assertFalse(row['totals_complete'])
        self.assertEqual(row['participation_status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
