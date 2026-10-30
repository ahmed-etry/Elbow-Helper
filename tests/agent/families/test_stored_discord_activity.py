"""Stored posting activity needs neither message text nor channel reads."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from elbow_helper.features.achievements.queries import AchievementQueries
from elbow_helper.agent.families.cohort_activity import discord_activity_projection
from agent.engine.test_agent_plan_flow import _context


class StoredDiscordActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_records_cover_observed_empty_missing_without_history_reads(self):
        connection = sqlite3.connect(':memory:', check_same_thread=False)
        connection.execute('CREATE TABLE user_stats (user_id INTEGER PRIMARY KEY, '
                           'last_activity_date INTEGER, activity_streak INTEGER, message_count INTEGER)')
        timestamp = int(datetime(2026, 1, 12, 17, 41, tzinfo=timezone.utc).timestamp())
        connection.executemany('INSERT INTO user_stats VALUES (?, ?, ?, ?)',
                               [(42, timestamp, 4, 129), (43, None, 0, 0)])
        queries = AchievementQueries(Path('unused.sqlite'))
        context = replace(_context(), achievement_queries=queries,
            message_search=SimpleNamespace(history_page=AsyncMock(
                side_effect=AssertionError('No channel reads permitted'))))
        rows = [{'member_id': identifier} for identifier in (42, 43, 44, None)]
        with patch.object(queries, '_connect', return_value=connection):
            coverage = await discord_activity_projection(context, rows)
        self.assertEqual([row['discord_activity']['status'] for row in rows],
                         ['observed', 'no_messages_recorded', 'no_record', 'unlinked'])
        self.assertEqual(rows[0]['discord_activity'], {
            'status': 'observed', 'last_active_day': '2026-01-12',
            'active_streak_days': 4, 'lifetime_messages': 129})
        self.assertIsNone(rows[2]['discord_activity']['lifetime_messages'])
        self.assertEqual(coverage['members_checked'], 3)
        self.assertNotIn('channel_ids', coverage)
        self.assertFalse(context.state.source_channels)
        context.message_search.history_page.assert_not_awaited()

    async def test_query_runs_off_event_loop_and_deduplicates_owners(self):
        loop_thread = threading.get_ident()
        calls = []

        def read(identifiers):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            calls.append(identifiers)
            return {}

        context = replace(_context(), achievement_queries=SimpleNamespace(member_activity=read))
        await discord_activity_projection(context, [{'member_id': 42}, {'member_id': 42}])
        self.assertEqual(calls, [[42]])

    async def test_missing_or_broken_source_preserves_unknown_counts(self):
        def broken(identifiers):
            raise RuntimeError('Synthetic read failure')

        for source in (None, SimpleNamespace(member_activity=broken)):
            with self.subTest(source=source):
                rows = [{'member_id': 42}]
                coverage = await discord_activity_projection(
                    replace(_context(), achievement_queries=source), rows)
                self.assertEqual(rows[0]['discord_activity']['status'], 'source_unavailable')
                self.assertIsNone(rows[0]['discord_activity']['lifetime_messages'])
                self.assertEqual(coverage['members_not_checked'], 1)

    def test_missing_database_and_invalid_schema_are_read_failures(self):
        path = Path('missing_activity_test.sqlite')
        self.assertFalse(path.exists())
        with self.assertRaises(RuntimeError):
            AchievementQueries(path).member_activity([42])
        self.assertFalse(path.exists())
        connection = sqlite3.connect(':memory:')
        connection.execute('CREATE TABLE unrelated (id INTEGER)')
        queries = AchievementQueries(path)
        with patch.object(queries, '_connect', return_value=connection):
            with self.assertRaises(RuntimeError):
                queries.member_activity([42])
