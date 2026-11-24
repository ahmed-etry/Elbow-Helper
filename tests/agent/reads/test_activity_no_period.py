"""Undated activity reads retain Discord facts and explicit unchecked war coverage."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.reads import identity_reads as reads
from elbow_helper.agent.reads.cohort_activity import roster_cohort
from tests.agent.reads.test_named_account_stats import lookup_context, named_member


class ActivityNoPeriodTests(unittest.IsolatedAsyncioTestCase):
    def context(self, source):
        context = lookup_context([named_member(42, 'Synthetic first'),
                                  named_member(43, 'Synthetic second')],
            [('#P0', 'Synthetic account', 42)])
        return replace(context, achievement_queries=source,
            message_search=SimpleNamespace(history_page=AsyncMock(
                side_effect=AssertionError('No message history reads'))))


    async def test_roster_without_period_preserves_unlinked_accounts_and_unchecked_counts(self):
        context = self.context(SimpleNamespace(member_activity=Mock(return_value={})))
        roster = {'rows': [{'clan_code': 'BES', 'player_tag': tag, 'donations': 3}
                           for tag in ('#P0', '#Q0')], 'count_complete': True, 'clans': []}
        with patch.object(reads, 'clan_rosters', AsyncMock(return_value=roster)), \
             patch('elbow_helper.agent.reads.cohort_activity.selected_wars', AsyncMock()) as wars:
            result = await roster_cohort(context, {'include_activity': True})
        wars.assert_not_awaited()
        self.assertEqual(result['account_count'], 2)
        self.assertEqual(result['member_count'], 1)
        self.assertEqual(result['clan_activity'][0]['accounts_not_checked'], 2)
        self.assertEqual(result['clan_activity'][0]['accounts_without_stored_participation'], 0)
        unlinked = next(row for row in result['rows'] if row['member_id'] is None)
        self.assertEqual(unlinked['discord_activity_status'], 'unlinked')
        self.assertEqual(unlinked['war_status'], 'not_checked')
