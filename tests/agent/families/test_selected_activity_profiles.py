"""Selected activity keeps account scope and compact configured owner profiles."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.families import identity_reads as reads
from elbow_helper.agent.families.core import family_tool
from elbow_helper.agent.families.member_context import owner_context, PROFILE_ROLE_GROUPS
from elbow_helper.agent.families.model_view import family_model_view
from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.agent.engine.assessment import assessment_limits
from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import MEMBER_ROLE_ID, CO_LEADER_ROLE_ID
from agent.families.test_named_account_stats import lookup_context, named_member


class SelectedActivityProfileTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        context = lookup_context([named_member(42, 'Synthetic one'), named_member(43, 'Synthetic two')],
            [(tag, 'Synthetic account', owner) for tag, owner in
             (('#P0', 42), ('#Q0', 42), ('#P2', 43), ('#Q2', 42))])
        activity = Mock(return_value={})
        return replace(context, achievement_queries=SimpleNamespace(member_activity=activity)), activity

    async def test_three_selected_accounts_two_owners_do_not_expand_to_other_alts(self):
        context, activity = self.context()
        accounts_tool, cohort_tool = reads.TOOLS[0], reads.TOOLS[2]
        data = {tag: {'status': 'ok', 'data': {'name': 'Synthetic', 'donations': 2}}
                for tag in ('#P0', '#Q0', '#P2', '#Q2')}
        with patch.object(reads, 'profiles', AsyncMock(return_value=data)):
            accounts = await accounts_tool.handler(context, {'account_tags': ['#P0', '#Q0', '#P2'], 'limit': 1})
        links = context.account_links.get_links_for_members
        links.reset_mock()
        wars = {'rows': [{'player_tag': tag, 'player_name': 'Synthetic', 'war_id': 'W1',
            'attacks_used': used, 'attacks_expected': used, 'missed': 0}
            for tag, used in (('#P0', 1), ('#Q0', 2), ('#P2', 3), ('#Q2', 99))]}
        with patch('elbow_helper.agent.families.cohort_activity.selected_wars', AsyncMock(return_value=wars)), \
             patch.object(reads, 'profiles', AsyncMock(side_effect=AssertionError('Do not expand owners'))):
            result = await cohort_tool.handler(context, {'basis': 'selected', 'cohort_ref': accounts['report_id'],
                'include_activity': True, 'after': '2025-12-01T00:00:00+00:00', 'view': 'count'})
        activity.assert_called_once_with([42, 43])
        links.assert_not_called()
        retained = json.loads(context.state.reports[result['report_id']].payload)['rows']
        self.assertEqual({a['player_tag'] for r in retained for a in r['accounts']}, {'#P0', '#Q0', '#P2'})
        self.assertEqual(sum(r['attacks_used'] for r in retained), 6)
        self.assertEqual((result['account_count'], result['member_count']), (3, 2))
        writer = await cohort_writer_evidence(context, {'activity': result})
        self.assertEqual(writer['activity']['total_rows'], 2)
        self.assertTrue(writer['activity']['rows'])
        self.assertNotIn('chat_coverage', result['activity_coverage'])

    async def test_member_ids_include_all_selected_members_accounts_and_departed_members(self):
        context, activity = self.context()
        with patch.object(reads, 'profiles', AsyncMock(return_value={})), \
             patch('elbow_helper.agent.families.cohort_activity.selected_wars', AsyncMock(return_value={'rows': []})):
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
        with patch('elbow_helper.agent.families.projections.authorize_report', AsyncMock(return_value=False)):
            result = await reads.member_cohort(context, {'basis': 'selected', 'cohort_ref': accounts['report_id']})
        self.assertEqual(result['cause'], 'report_unavailable')
        activity.assert_not_called()

    async def test_roles_opt_in_and_only_configured_groups_in_retained_and_model_profiles(self):
        context, _ = self.context()
        member = context.guild.get_member(42)
        allowed = {CLANS['BES'].member_role_id, CLANS['BES'].war_role_id, MEMBER_ROLE_ID}
        member.roles = [SimpleNamespace(id=identifier, name='Configured') for identifier in allowed]
        member.roles.append(SimpleNamespace(id=123, name='Cosmetic separator'))
        self.assertNotIn('leadership', owner_context(member, 42))
        self.assertNotIn('leadership', reads.member_fields(None, 404))
        with patch.object(reads, 'profiles', AsyncMock(return_value={})):
            hidden = await reads.TOOLS[0].handler(context, {'member_ids': [42]})
            shown = await reads.TOOLS[0].handler(context, {'member_ids': [42], 'include_roles': True})
        self.assertNotIn('roles', hidden['members'][0])
        self.assertNotIn('leadership', hidden['members'][0])
        for value in (shown, family_model_view(shown)):
            self.assertEqual({r['role_id'] for r in value['members'][0]['roles']}, allowed)
            self.assertEqual(value['configured_role_groups'], list(PROFILE_ROLE_GROUPS))
        member.roles.append(SimpleNamespace(id=CO_LEADER_ROLE_ID, name='Leadership'))
        self.assertEqual(owner_context(member, 42)['leadership']['status'], 'active')

    def test_absent_leadership_keeps_unknown_owner_filters_without_marking_present_members(self):
        result = assessment_limits({'cohort': {'leadership_filter': {'unverified_accounts': 1},
            'rows': [{'member_id': identifier, 'membership_status': status,
                      'accounts': [{'player_tag': tag, 'player_name': 'Synthetic'}]}
                     for identifier, status, tag in ((42, 'present', '#P0'), (404, 'not_in_guild', '#Q0'))]}})
        self.assertEqual([r['player_tag'] for r in result['unverified_selection']], ['#Q0'])
