"""Chosen accounts retain their scope; member profiles retain useful roles only."""
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
from elbow_helper.agent.families.projections import performance_projection
from elbow_helper.configuration.roles import MEMBER_ROLE_ID, CO_LEADER_ROLE_ID, AGE_ROLE_IDS
from agent.families.test_named_account_stats import lookup_context, named_member


class ChosenActivityProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_selected_accounts_two_owners_never_expand_to_other_alts(self):
        context = lookup_context([named_member(42, 'Owner A'), named_member(43, 'Owner B')], [
            ('#P0', 'Account A', 42), ('#P2', 'Account B', 42),
            ('#P8', 'Account C', 43), ('#P9', 'Other account', 42)])
        source = next(tool for tool in reads.TOOLS if tool.definition.name == 'read_member_accounts')
        values = {tag: {'status': 'ok', 'data': {'name': tag, 'donations': 2}}
                  for tag in ('#P0', '#P2', '#P8', '#P9')}
        with patch.object(reads, 'profiles', AsyncMock(return_value=values)):
            selection = await source.handler(context, {'account_tags': ['#P0', '#P2', '#P8'], 'limit': 1})
        retained = context.state.reports[selection['report_id']].payload
        context.account_links.get_links_for_members.reset_mock()
        activity = Mock(return_value={})
        context = replace(context, achievement_queries=SimpleNamespace(member_activity=activity))
        wars = [{'player_tag': tag, 'player_name': tag, 'war_id': 'W',
                 'war_type': 'REG', 'attacks_used': count, 'attacks_expected': count, 'missed': 0}
                for tag, count in (('#P0', 1), ('#P2', 2), ('#P8', 1), ('#P9', 99))]
        tool = next(tool for tool in reads.TOOLS if tool.definition.name == 'read_member_cohort')
        with patch('elbow_helper.agent.families.cohort_activity.selected_wars',
                   AsyncMock(return_value={'rows': wars})), \
             patch('elbow_helper.agent.families.war_reads.selected_wars',
                   AsyncMock(return_value={'rows': wars})):
            result = await tool.handler(context, {'basis': 'selected', 'cohort_ref': selection['report_id'],
                'include_activity': True, 'performance': {'war_type': 'REG'},
                'after': '2025-12-01T00:00:00+00:00', 'view': 'count'})
        self.assertNotIn('error', result)
        snapshot = json.loads(context.state.reports[result['report_id']].payload)
        rows = snapshot['rows']
        self.assertEqual({account['player_tag'] for row in rows for account in row['accounts']},
                         {'#P0', '#P2', '#P8'})
        self.assertEqual({row['member_id']: row['attacks_used'] for row in rows}, {42: 3, 43: 1})
        self.assertEqual({row['member_id']: row['war_attendance']['attacks_used'] for row in rows},
                         {42: 3, 43: 1})
        activity.assert_called_once_with([42, 43])
        context.account_links.get_links_for_members.assert_not_called()
        self.assertEqual(context.state.reports[selection['report_id']].payload, retained)
        writer = await cohort_writer_evidence(context, {'activity': result})
        table = writer['activity']['rows']
        self.assertEqual(len(table['rows'] if isinstance(table, dict) else table), 2)

    async def test_chosen_unlinked_accounts_survive_without_any_owners(self):
        context = lookup_context()
        source = family_tool('read_member_accounts', 'Synthetic accounts', {}, AsyncMock(return_value={
            'rows': [{'member_id': None, 'player_tag': '#P0', 'profile_status': 'ok', 'donations': 2}]}))
        accounts = await source.handler(context, {})
        with patch('elbow_helper.agent.families.cohort_activity.selected_wars',
                   AsyncMock(return_value={'error': 'Unavailable', 'cause': 'source_unavailable'})):
            result = await reads.member_cohort(context, {'basis': 'selected', 'cohort_ref': accounts['report_id'],
                'include_activity': True, 'after': '2025-12-01T00:00:00+00:00'})
        self.assertEqual((result['account_count'], result['member_count']), (1, 0))
        self.assertEqual(result['rows'][0]['account_tags'], ['#P0'])
        self.assertEqual(result['rows'][0]['discord_activity_status'], 'unlinked')
        self.assertEqual(result['rows'][0]['war_status'], 'source_unavailable')

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
        with patch('elbow_helper.agent.families.projections.authorize_report', AsyncMock(return_value=False)):
            result = await reads.member_cohort(context, {'basis': 'selected', 'cohort_ref': accounts['report_id']})
        self.assertEqual(result['cause'], 'report_unavailable')

    def test_leadership_only_exists_for_role_holders(self):
        owner = named_member(42, 'Owner A')
        self.assertNotIn('leadership', reads.member_fields(owner, 42))
        self.assertNotIn('leadership', owner_context(None, 404))
        owner.roles = [SimpleNamespace(id=CO_LEADER_ROLE_ID, name='Leader')]
        self.assertEqual(reads.member_fields(owner, 42)['leadership']['status'], 'active')

    def test_selection_uncertainty_only_marks_missing_owners(self):
        result = assessment_limits({'cohort': {
            'leadership_filter': {'unverified_accounts': 1},
            'rows': [{'member_id': 42, 'membership_status': 'present',
                      'accounts': [{'player_tag': '#P0'}]},
                     {'member_id': None, 'membership_status': 'unlinked',
                      'accounts': [{'player_tag': '#P9'}]}]}})
        self.assertEqual([row['player_tag'] for row in result['unverified_selection']], ['#P9'])

    async def test_performance_scope_excludes_other_accounts_of_selected_owner(self):
        context = lookup_context([named_member(42, 'Owner A')],
                                 [('#P0', 'Selected', 42), ('#P9', 'Other account', 42)])
        rows = [{'member_id': 42, 'accounts': [{'player_tag': '#P0'}]}]
        wars = [{'player_tag': tag, 'player_name': tag, 'war_id': 'W', 'war_type': 'REG',
                 'attacks_used': used, 'attacks_expected': used, 'missed': 0}
                for tag, used in (('#P0', 1), ('#P9', 99))]
        with patch('elbow_helper.agent.families.war_reads.selected_wars',
                   AsyncMock(return_value={'rows': wars})):
            await performance_projection(context, rows, {'war_type': 'REG'}, account_tags={'#P0'})
        self.assertEqual(rows[0]['war_attendance']['attacks_used'], 1)
        self.assertEqual(rows[0]['war_attendance']['account_tags'], ['#P0'])

    async def test_roles_require_opt_in_and_only_include_configured_operational_groups(self):
        owner = named_member(42, 'Owner A')
        owner.roles = [SimpleNamespace(id=identifier, name=f'Role {identifier}')
                       for identifier in (MEMBER_ROLE_ID, next(iter(AGE_ROLE_IDS)), 123)]
        context = lookup_context([owner])
        for include_roles in (False, True):
            result = await reads.member_accounts(context, {'member_ids': [42], 'include_roles': include_roles})
            profile = result['members'][0]
            visible = family_model_view({**result, 'family': 'read_member_accounts',
                                         'selection': {'include_roles': include_roles}})['members'][0]
            for row in (profile, visible):
                if include_roles:
                    self.assertEqual([role['role_id'] for role in row['roles']], [MEMBER_ROLE_ID])
                else:
                    self.assertNotIn('roles', row)
            if include_roles:
                self.assertEqual(set(result['configured_role_groups']), set(PROFILE_ROLE_GROUPS))
