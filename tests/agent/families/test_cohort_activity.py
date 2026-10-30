"""Activity joins preserve the full roster and never classify message content."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.families.cohort_activity import roster_cohort
from elbow_helper.agent.families.cohort_activity import discord_activity_projection
from elbow_helper.agent.families.cohort_activity import war_measures
from elbow_helper.features.clan_health.attendance import aggregate_attendance
from elbow_helper.agent.families.model_view import family_model_view
from elbow_helper.features.achievements.queries import AchievementQueries
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.plan.checker import check_plan
from agent.engine.test_agent_plan_flow import _context


class CohortActivityTests(unittest.IsolatedAsyncioTestCase):
    def activity_database(self):
        database = Path(f'.activity-test-{uuid4().hex}.sqlite')
        self.addCleanup(database.unlink, missing_ok=True)
        return database

    def test_completion_uses_confirmed_attack_opportunities_and_preserves_unknowns(self):
        source = [dict(player_tag='#P0', player_name='First', war_id='R1',
                       attacks_used=1, attacks_expected=2, missed=1),
                  dict(player_tag='#P1', player_name='Second', war_id='C1',
                       attacks_used=1, attacks_expected=1, missed=0)]
        observed = aggregate_attendance(source, {}, 'account')
        result = war_measures(observed)
        self.assertEqual((result['attacks_used'], result['attacks_expected'],
                          result['missed'], result['attack_completion_percent']), (2, 3, 1, 66.67))
        zero = aggregate_attendance([dict(source[0], attacks_used=0, missed=2)], {}, 'account')
        self.assertEqual(war_measures(zero)['attack_completion_percent'], 0)
        incomplete = aggregate_attendance([dict(source[0], attacks_expected=None, missed=None)], {}, 'account')
        for observations in ([], incomplete, observed + incomplete):
            with self.subTest(observations=observations):
                self.assertIsNone(war_measures(observations)['attack_completion_percent'])
                self.assertIsNone(war_measures(observations)['attacks_expected'])
        self.assertEqual(war_measures([], unavailable=True)['status'], 'source_unavailable')
        self.assertIsNone(war_measures([], unavailable=True)['wars_observed'])

    async def test_stored_activity_covers_records_without_channel_reads(self):
        database = self.activity_database()
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('CREATE TABLE user_stats (user_id INTEGER PRIMARY KEY, '
                'last_activity_date INTEGER, activity_streak INTEGER, message_count INTEGER)')
            connection.executemany('INSERT INTO user_stats VALUES (?, ?, ?, ?)', [
                (42, int(datetime(2025, 12, 15, 8, 30, tzinfo=timezone.utc).timestamp()), 4, 123),
                (43, None, 0, 0)])
        queries = AchievementQueries(database)
        event_thread = threading.get_ident()
        original = queries.member_activity
        def read(identifiers):
            self.assertNotEqual(threading.get_ident(), event_thread)
            return original(identifiers)
        queries.member_activity = read
        history = AsyncMock(side_effect=AssertionError('No channel history allowed'))
        context = replace(_context(), achievement_queries=queries,
            message_search=SimpleNamespace(history_page=history))
        rows = [{'member_id': identifier} for identifier in (42, 43, 44, None)]
        coverage = await discord_activity_projection(context, rows)
        self.assertEqual([r['discord_activity']['status'] for r in rows],
                         ['observed', 'no_messages_recorded', 'no_record', 'unlinked'])
        self.assertEqual(rows[0]['discord_activity'], {'status': 'observed',
            'last_active_day': '2025-12-15', 'active_streak_days': 4, 'lifetime_messages': 123})
        self.assertTrue(coverage['source_reads_complete'])
        self.assertEqual(coverage['members_checked'], 3)
        self.assertFalse(context.state.source_channels)
        self.assertFalse(any(key.startswith('channel') for key in coverage))
        history.assert_not_awaited()

    async def test_missing_and_failed_activity_store_remain_unknown(self):
        for queries in (None, SimpleNamespace(member_activity=lambda ids: (_ for _ in ()).throw(
                RuntimeError('Synthetic unreadable database')))):
            rows = [{'member_id': 42}]
            coverage = await discord_activity_projection(replace(_context(), achievement_queries=queries), rows)
            self.assertEqual(rows[0]['discord_activity']['status'], 'source_unavailable')
            self.assertFalse(coverage['source_reads_complete'])
            self.assertEqual(coverage['members_not_checked'], 1)

    def test_missing_malformed_and_read_only_activity_databases(self):
        database = self.activity_database()
        queries = AchievementQueries(database)
        with self.assertRaises(RuntimeError):
            queries.member_activity([42])
        self.assertFalse(database.exists())
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('CREATE TABLE unrelated (value INTEGER)')
        with self.assertRaises(RuntimeError):
            queries.member_activity([42])
        with closing(queries._connect()) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('CREATE TABLE forbidden (value INTEGER)')

    def test_initial_catalogue_supports_chosen_accounts_and_has_no_scan_continuation(self):
        registry = {name: tool for name, tool in build_agent_tools().items() if tool.default_visible}
        plan = {'goal': 'Read selected member activity', 'effort': 'high', 'output': 'text', 'steps': [
            {'id': 'accounts', 'capability': 'read_member_accounts', 'arguments': {'member_ids': [42]}, 'depends_on': []},
            {'id': 'activity', 'capability': 'read_member_cohort', 'arguments': {'basis': 'selected',
                'cohort_ref': {'step': 'accounts', 'path': ['report_id']}, 'include_activity': True,
                'after': '2025-12-01T00:00:00+00:00', 'view': 'count'}, 'depends_on': ['accounts']}]}
        checked = check_plan(plan, registry)
        self.assertTrue(checked.ok, checked.error)
        self.assertNotIn('activity_ref', registry['read_member_cohort'].definition.parameters['properties'])

    async def test_accounts_join_across_clans_and_unlinked_accounts_survive(self):
        context = _context()
        owner = SimpleNamespace(id=42, display_name='Another member', roles=[], joined_at=None)
        context.guild.get_member = lambda identifier: owner if identifier == 42 else context.member
        roster = {'rows': [dict(clan_code=code, player_tag=tag, player_name=tag,
            donations=donations, townHallLevel=17, profile_status='ok')
            for code, tag, donations in [('BES', '#P0', 12), ('BEM', '#P2', 3), ('BEM', '#P8', None)]],
            'count_complete': True, 'clans': []}
        links = SimpleNamespace(get_links_by_tags=lambda tags: {
            tag: {'discord_user_id': 42} for tag in ('#P0', '#P2')})
        search = SimpleNamespace(history_page=AsyncMock(side_effect=AssertionError('No channel reads')))
        context = replace(context, account_links=links, message_search=search,
            achievement_queries=SimpleNamespace(member_activity=lambda ids: {42: {'status': 'observed',
                'last_active_day': '2025-12-15', 'active_streak_days': 2, 'lifetime_messages': 9}}))
        war_rows = [dict(player_tag='#P0', player_name='Account', war_id='W1',
                        attacks_used=2, attacks_expected=2, missed=0),
                    dict(player_tag='#P0', player_name='Account', war_id='W2',
                         attacks_used=0, attacks_expected=1, missed=1)]
        with patch('elbow_helper.agent.families.identity_reads.clan_rosters', AsyncMock(return_value=roster)), \
             patch('elbow_helper.agent.families.cohort_activity.selected_wars', AsyncMock(return_value={'rows': war_rows, 'coverage': {'historical_collection_complete': False}})):
            result = await roster_cohort(context, {'include_activity': True, 'after': '2025-12-01T00:00:00+00:00'})
        self.assertEqual(result['account_count'], 3)
        self.assertEqual(result['member_count'], 1)
        self.assertEqual(len(result['rows']), 2)
        member, unlinked = result['rows']
        self.assertEqual(member['member_id'], 42)
        self.assertEqual(member['donations'], 15)
        self.assertEqual(member['war_activity']['attacks_used'], 2)
        self.assertEqual(member['war_activity']['wars_observed'], 2)
        self.assertEqual(member['war_activity']['attacks_expected'], 3)
        self.assertEqual(member['war_activity']['attack_completion_percent'], 66.67)
        self.assertEqual(member['discord_activity']['status'], 'observed')
        self.assertIsNone(unlinked['donations'])
        self.assertIsNone(unlinked['war_activity']['attacks_used'])
        self.assertEqual(unlinked['discord_activity']['status'], 'unlinked')
        search.history_page.assert_not_awaited()
        visible = family_model_view({**result, 'family': 'read_member_cohort'})
        self.assertEqual({tag for r in visible['rows'] for tag in r['account_tags']}, {'#P0', '#P2', '#P8'})
        self.assertEqual(visible['rows'][0]['donations'], 15)
        accounts = {account['player_tag']: account for account in visible['rows'][0]['accounts']}
        self.assertEqual(accounts['#P0']['war_activity']['attacks_used'], 2)
        self.assertEqual(accounts['#P0']['war_activity']['attacks_expected'], 3)
        self.assertEqual(accounts['#P0']['war_activity']['missed'], 1)
        self.assertEqual(accounts['#P0']['war_activity']['attack_completion_percent'], 66.67)
        self.assertEqual(visible['rows'][0]['attack_completion_percent'], 66.67)
        self.assertIsNone(accounts['#P2']['war_activity']['attacks_used'])
        self.assertIsNone(accounts['#P2']['war_activity']['attack_completion_percent'])
        self.assertEqual(accounts['#P2']['war_activity']['status'], 'no_stored_participation')
        by_clan = {row['clan_code']: row for row in result['clan_activity']}
        self.assertEqual(by_clan['BES']['accounts_with_observed_attacks'], 1)
        self.assertEqual(by_clan['BEM']['accounts_with_observed_attacks'], 0)
        self.assertEqual(by_clan['BEM']['accounts_without_stored_participation'], 2)

    async def test_invalid_window_is_rejected_before_activity_queries(self):
        context = _context()
        roster = {'rows': [], 'count_complete': True, 'clans': []}
        with patch('elbow_helper.agent.families.identity_reads.clan_rosters', AsyncMock(return_value=roster)), \
             patch('elbow_helper.agent.families.cohort_activity.selected_wars', AsyncMock()) as wars:
            invalid = await roster_cohort(context, {'include_activity': True, 'after': '2026-02-01T00:00:00+00:00'})
        self.assertEqual(invalid['cause'], 'invalid_arguments')
        wars.assert_not_awaited()
