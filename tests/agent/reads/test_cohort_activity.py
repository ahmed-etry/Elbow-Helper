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
from unittest.mock import AsyncMock

from elbow_helper.agent.reads.cohort_activity import discord_activity_projection
from elbow_helper.agent.reads.cohort_activity import war_measures
from elbow_helper.domain.war_participation import aggregate_participation
from elbow_helper.features.achievements.queries import AchievementQueries
from tests.agent.request_fixtures import flow_context as _context


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
        observed = aggregate_participation(source, {}, 'account')
        result = war_measures(observed)
        self.assertEqual((result['attacks_used'], result['attacks_expected'],
                          result['missed'], result['attack_completion_percent']), (2, 3, 1, 66.67))
        zero = aggregate_participation([dict(source[0], attacks_used=0, missed=2)], {}, 'account')
        self.assertEqual(war_measures(zero)['attack_completion_percent'], 0)
        incomplete = aggregate_participation([dict(source[0], attacks_expected=None, missed=None)], {}, 'account')
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
