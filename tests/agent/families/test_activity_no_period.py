"""Undated activity reads retain Discord facts and explicit unchecked war coverage."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.agent.families import identity_reads as reads
from elbow_helper.agent.families.cohort_activity import roster_cohort
from agent.families.test_named_account_stats import lookup_context, named_member


class ActivityNoPeriodTests(unittest.IsolatedAsyncioTestCase):
    def context(self, source):
        context = lookup_context([named_member(42, 'Synthetic first'),
                                  named_member(43, 'Synthetic second')],
            [('#P0', 'Synthetic account', 42)])
        return replace(context, achievement_queries=source,
            message_search=SimpleNamespace(history_page=AsyncMock(
                side_effect=AssertionError('No message history reads'))))

    async def test_selected_members_reach_writer_with_stored_activity_without_after(self):
        records = {identifier: {'status': 'observed', 'last_active_day': day,
            'active_streak_days': streak, 'lifetime_messages': total}
            for identifier, day, streak, total in
            ((42, '2025-07-24', 4, 2345), (43, '2025-08-03', 2, 1456))}
        activity = Mock(return_value=records)
        context = self.context(SimpleNamespace(member_activity=activity))
        with patch.object(reads, 'profiles', AsyncMock(return_value={})), \
             patch('elbow_helper.agent.families.cohort_activity.selected_wars', AsyncMock()) as wars:
            result = await reads.TOOLS[2].handler(context, {'basis': 'selected',
                'member_ids': [42, 43, 404], 'include_activity': True})
        self.assertNotIn('error', result)
        wars.assert_not_awaited()
        activity.assert_called_once_with([42, 43, 404])
        writer = answer_evidence(await cohort_writer_evidence(context, {'activity': result}))['activity']
        rows = {row['member_id']: row for row in writer['rows']}
        self.assertEqual(set(rows), {42, 43, 404})
        stored_rows = {row['member_id']: row for row in result['rows']}
        for identifier, record in records.items():
            self.assertEqual(stored_rows[identifier]['discord_activity'], record)
            for field in ('last_active_day', 'active_streak_days', 'lifetime_messages'):
                self.assertEqual(rows[identifier][field], record[field])
        self.assertEqual(rows[404]['discord_activity_status'], 'no_record')
        for row in rows.values():
            self.assertEqual(row['war_status'], 'not_checked')
            self.assertIsNone(row['wars_observed'])
            self.assertIsNone(row['attacks_used'])
            for account in row['accounts']:
                self.assertEqual(account['war_activity']['status'], 'not_checked')
        coverage = writer['activity_coverage']
        self.assertIsNone(coverage['period'])
        self.assertEqual(coverage['war_source_status'], 'not_checked')
        self.assertEqual(coverage['war_coverage']['reason'], 'no_period')
        self.assertEqual(coverage['discord_activity_coverage']['members_checked'], 3)
        context.message_search.history_page.assert_not_awaited()

    async def test_failed_or_missing_discord_store_without_period_stays_unavailable(self):
        for source in (None, SimpleNamespace(member_activity=Mock(
                side_effect=RuntimeError('Synthetic read failure')))):
            with self.subTest(source=source):
                context = self.context(source)
                with patch.object(reads, 'profiles', AsyncMock(return_value={})), \
                     patch('elbow_helper.agent.families.cohort_activity.selected_wars', AsyncMock()) as wars:
                    result = await reads.member_cohort(context, {'basis': 'selected',
                        'member_ids': [42, 43], 'include_activity': True})
                self.assertNotIn('error', result)
                self.assertEqual([row['discord_activity_status'] for row in result['rows']],
                                 ['source_unavailable', 'source_unavailable'])
                self.assertTrue(all(row['lifetime_messages'] is None for row in result['rows']))
                self.assertEqual(result['activity_coverage']['discord_activity_coverage']['members_not_checked'], 2)
                wars.assert_not_awaited()

    async def test_roster_without_period_preserves_unlinked_accounts_and_unchecked_counts(self):
        context = self.context(SimpleNamespace(member_activity=Mock(return_value={})))
        roster = {'rows': [{'clan_code': 'BES', 'player_tag': tag, 'donations': 3}
                           for tag in ('#P0', '#Q0')], 'count_complete': True, 'clans': []}
        with patch.object(reads, 'clan_rosters', AsyncMock(return_value=roster)), \
             patch('elbow_helper.agent.families.cohort_activity.selected_wars', AsyncMock()) as wars:
            result = await roster_cohort(context, {'include_activity': True})
        wars.assert_not_awaited()
        self.assertEqual(result['account_count'], 2)
        self.assertEqual(result['member_count'], 1)
        self.assertEqual(result['clan_activity'][0]['accounts_not_checked'], 2)
        self.assertEqual(result['clan_activity'][0]['accounts_without_stored_participation'], 0)
        unlinked = next(row for row in result['rows'] if row['member_id'] is None)
        self.assertEqual(unlinked['discord_activity_status'], 'unlinked')
        self.assertEqual(unlinked['war_status'], 'not_checked')
