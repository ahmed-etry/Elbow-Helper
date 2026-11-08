"""Selected activity keeps account scope and compact configured owner profiles."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.reads import identity_reads as reads
from elbow_helper.agent.tool_support.core import family_tool
from elbow_helper.agent.engine.assessment import assessment_limits
from agent.families.test_named_account_stats import lookup_context, named_member


class SelectedActivityProfileTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        context = lookup_context([named_member(42, 'Synthetic one'), named_member(43, 'Synthetic two')],
            [(tag, 'Synthetic account', owner) for tag, owner in
             (('#P0', 42), ('#Q0', 42), ('#P2', 43), ('#Q2', 42))])
        activity = Mock(return_value={})
        return replace(context, achievement_queries=SimpleNamespace(member_activity=activity)), activity


    async def test_member_ids_include_all_selected_members_accounts_and_departed_members(self):
        context, activity = self.context()
        with patch.object(reads, 'profiles', AsyncMock(return_value={})), \
             patch('elbow_helper.agent.reads.cohort_activity.selected_wars', AsyncMock(return_value={'rows': []})):
            result = await reads.member_cohort(context, {'basis': 'selected', 'member_ids': [42, 404],
                'include_activity': True, 'after': '2025-12-01T00:00:00+00:00'})
        self.assertEqual({r['member_id'] for r in result['rows']}, {42, 404})
        self.assertEqual(result['account_count'], 3)
        activity.assert_called_once_with([42, 404])
        self.assertEqual(next(r for r in result['rows'] if r['member_id'] == 404)['membership_status'], 'not_in_guild')

    async def test_invalid_and_unauthorized_references_make_no_activity_reads(self):
        context, activity = self.context()
        for arguments in ({}, {'member_ids': [42], 'cohort_ref': 'missing'}, {'cohort_ref': 'missing'}):
            result = await reads.member_cohort(context, {'basis': 'selected', **arguments})
            self.assertIn(result['cause'], ('invalid_arguments', 'report_unavailable'))
        wrong = family_tool('read_member_cohort', 'Synthetic', {}, AsyncMock(return_value={
            'rows': [{'member_id': 42}]}))
        page = await wrong.handler(context, {})
        result = await reads.member_cohort(context, {'basis': 'selected', 'cohort_ref': page['report_id']})
        self.assertEqual(result['cause'], 'invalid_arguments')
        accounts = await reads.TOOLS[0].handler(context, {'member_ids': [404]})
        with patch('elbow_helper.agent.reads.projections.authorize_report', AsyncMock(return_value=False)):
            result = await reads.member_cohort(context, {'basis': 'selected', 'cohort_ref': accounts['report_id']})
        self.assertEqual(result['cause'], 'report_unavailable')
        activity.assert_not_called()


    def test_absent_leadership_keeps_unknown_owner_filters_without_marking_present_members(self):
        result = assessment_limits({'cohort': {'leadership_filter': {'unverified_accounts': 1},
            'rows': [{'member_id': identifier, 'membership_status': status,
                      'accounts': [{'player_tag': tag, 'player_name': 'Synthetic'}]}
                     for identifier, status, tag in ((42, 'present', '#P0'), (404, 'not_in_guild', '#Q0'))]}})
        self.assertEqual([r['player_tag'] for r in result['unverified_selection']], ['#Q0'])
