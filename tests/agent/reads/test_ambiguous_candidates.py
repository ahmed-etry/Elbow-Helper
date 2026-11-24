"""Ambiguous public reads cover candidates without authorizing a subject."""

import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads import account_names, identity_reads
from tests.agent.reads.test_named_account_stats import lookup_context, named_member, rosters


class AmbiguousCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_game_candidates_keep_query_provenance_and_unknown_miss_needs_input(self):
        context = lookup_context([named_member(42, 'Amber Pilot'), named_member(43, 'Copper Pilot')],
                                 [('#P0', 'Silver Kite', 42), ('#Q0', 'Silver Kite', 43)])
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())), \
             patch.object(identity_reads, 'profiles', AsyncMock(return_value={})):
            for queries, expected in ((['Silver Kite'], 'resolved'),
                                      (['Silver Kite', 'Absent Subject'], 'needs_input')):
                result = await identity_reads.member_accounts(context, {
                    'account_names': queries, 'look_in': 'game'})
                self.assertEqual(result['status'], expected)
                self.assertEqual([row['player_tag'] for row in result['rows']], ['#P0', '#Q0'])
                self.assertTrue(all(row['candidate_for'] == ['Silver Kite'] for row in result['rows']))
                self.assertEqual(result['resolved_member_ids'], [])
                self.assertEqual(context.state.resolved_members, {})
            empty = await identity_reads.member_accounts(context, {'name': 'Absent Subject', 'look_in': 'game'})
        self.assertEqual(empty['status'], 'needs_input')
        self.assertEqual(empty['rows'], [])
