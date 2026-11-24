"""Chosen accounts retain their scope; member profiles retain useful roles only."""
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads import identity_reads as reads
from elbow_helper.agent.reads.snapshots import family_tool
from tests.agent.reads.test_named_account_stats import lookup_context, named_member


class ChosenActivityProfileTests(unittest.IsolatedAsyncioTestCase):

    async def test_explicit_member_ids_include_departed_and_unlinked_members(self):
        context = lookup_context([named_member(42, 'Owner A')])
        result = await reads.member_cohort(context, {'basis': 'selected', 'member_ids': [42, 404]})
        self.assertEqual({row['member_id'] for row in result['rows']}, {42, 404})
        self.assertEqual(result['member_count'], 2)
        self.assertEqual(result['account_count'], 0)
        self.assertEqual(next(row for row in result['rows'] if row['member_id'] == 404)
                         ['membership_status'], 'not_in_guild')

    async def test_invalid_and_unauthorized_selections_fail_before_metrics(self):
        context = lookup_context()
        roster = family_tool('read_clan_rosters', 'Synthetic roster', {},
                            AsyncMock(return_value={'rows': [{'player_tag': '#P0'}]}))
        report = await roster.handler(context, {})
        for selection in ({}, {'member_ids': [42], 'cohort_ref': report['report_id']},
                          {'cohort_ref': 'missing'}, {'cohort_ref': report['report_id']}):
            with self.subTest(selection=selection):
                result = await reads.member_cohort(context, {'basis': 'selected', **selection})
                self.assertIn('error', result)
        account_tool = family_tool('read_member_accounts', 'Synthetic accounts', {},
            AsyncMock(return_value={'rows': [{'player_tag': '#P0', 'member_id': 42}]}))
        accounts = await account_tool.handler(context, {})
        with patch('elbow_helper.agent.reads.projections.authorize_report', AsyncMock(return_value=False)):
            result = await reads.member_cohort(context, {'basis': 'selected', 'cohort_ref': accounts['report_id']})
        self.assertEqual(result['cause'], 'report_unavailable')
