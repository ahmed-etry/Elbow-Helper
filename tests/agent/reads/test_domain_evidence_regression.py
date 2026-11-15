"""Identity evidence carries policy unknowns without exposing testing roles."""

from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID, MEMBER_ROLE_ID, TRIAL_ROLE_ID
from elbow_helper.agent.reads import account_activity, identity_reads
from tests.agent.engine.test_agent_plan_flow import _context
from tests.agent.reads.test_compact_family_regressions import delivered, unpack
from tests.agent.reads.test_named_account_stats import lookup_context, named_member


class DomainEvidenceRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_accounts_without_profiles_are_named_once_beside_their_activity_rows(self):
        alphabet = '0289PYLQGRJCUV'
        tags = ['#P' + alphabet[i // len(alphabet)] + alphabet[i % len(alphabet)] for i in range(17)]
        owners = {tag: 51 if index == 0 else 52 for index, tag in enumerate(tags)}
        context = lookup_context([named_member(51, 'Synthetic One'), named_member(52, 'Synthetic Many')],
                                 [(tag, 'Account ' + tag, owner) for tag, owner in owners.items()])
        context.source_message.created_at = datetime(2026, 11, 15, tzinfo=timezone.utc)
        activity = {kind: {'status': 'empty', 'attacks_used': 0, 'attack_performance': {
            metric: {'numerator': 0, 'denominator': 0, 'value': None, 'display': 'unavailable'}
            for metric in ('average_stars', 'triple_rate_percent', 'average_destruction_percent')}}
            for kind in ('regular_war', 'cwl', 'ranked')}

        async def history(context, rows, arguments):
            for row in rows:
                row['activity'] = activity
            return {kind: {'read_ran': True, 'status': 'complete'} for kind in activity}

        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        unavailable = {tag: {'tag': tag, 'status': 'unavailable'} for tag in tags}
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=unavailable)), \
                patch.object(account_activity, 'account_activity', history):
            page = await tool.handler(context, {'member_ids': [51, 52], 'after': '2026-10-01T00:00:00+00:00'})
        self.assertEqual(page['total_rows'], 17)
        view = delivered(page)
        rows = unpack(view['rows'])
        self.assertEqual(unpack(view['unknown_accounts']),
                         [{'member_id': row['member_id'], 'player_tag': row['player_tag']} for row in rows])
        self.assertTrue(all(row['activity'] == activity for row in rows))

    async def test_join_date_cohort_states_family_role_ids_once_without_losing_member_facts(self):
        members = [named_member(100 + index, f'Synthetic {index}') for index in range(53)]
        for index, member in enumerate(members):
            member.joined_at = datetime(2026, 10, 2, tzinfo=timezone.utc)
            member.roles = [SimpleNamespace(id=MEMBER_ROLE_ID if index % 2 else TRIAL_ROLE_ID,
                                            name='Synthetic role')]
        context = lookup_context(members)
        context.member.joined_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for role in context.member.roles:
            role.name = 'Synthetic leadership'
        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_cohort')
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value={})):
            result = await tool.handler(context, {'basis': 'joined_discord',
                                                  'joined_after': '2026-10-01T00:00:00+00:00'})
        view = delivered(result)
        self.assertEqual(view['family_membership_role_ids'], {'member': MEMBER_ROLE_ID, 'trial': TRIAL_ROLE_ID})
        rows = unpack(view['rows'])
        for row in rows:
            index = row['member_id'] - 100
            self.assertEqual(row['family_membership_roles'], {
                'member': {'held': bool(index % 2)}, 'trial': {'held': not index % 2}})
            self.assertEqual(row['home_clan_codes'], [])
            self.assertEqual(row['joined_at'], '2026-10-02T00:00:00+00:00')
            self.assertEqual(row['accounts'], [{'player_tag': None, 'profile_status': 'unlinked'}])
        self.assertEqual(len(rows), 53)
        retained = json.loads(context.state.reports[result['report_id']].payload)
        self.assertTrue(all(row['family_membership_roles']['member']['role_id'] == MEMBER_ROLE_ID
                            and row['family_membership_roles']['trial']['role_id'] == TRIAL_ROLE_ID
                            for row in retained['rows']))

    async def test_current_account_clan_does_not_resolve_unknown_operational_policy(self):
        context = replace(_context(), account_links=SimpleNamespace(
            get_links_for_members=lambda ids: {41: [{'player_tag': '#P0'}]}))
        for role in context.member.roles:
            role.name = 'Member-facing role'
            role.is_default = lambda: False
        context.member.roles.append(SimpleNamespace(id=AGENT_TESTER_ROLE_ID,
            name='Internal test label', is_default=lambda: False))
        tool = next(tool for tool in identity_reads.TOOLS if tool.definition.name == 'read_member_accounts')
        profiles = {'#P0': {'status': 'ok', 'data': {'name': 'Account', 'clan': {
            'tag': CLANS['BEP'].tag, 'name': 'Current utility clan'}}}}
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=profiles)):
            result = await tool.handler(context, {'member_ids': [41]})
        visible = delivered(result)
        self.assertIsNone(visible['clan_facts'][0]['operational_cwl_only'])
        self.assertFalse(visible['clan_facts'][0]['configured_cwl_only'])
        self.assertEqual(visible['rows'][0]['clan']['name'], 'Current utility clan')
        self.assertNotIn(AGENT_TESTER_ROLE_ID, [role['role_id'] for role in visible['rows'][0].get('roles', [])])
        self.assertNotIn('Internal test label', str(visible))
        self.assertNotIn('roles', result['members'][0])
        profiles['#P0']['status'] = 'unavailable'
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=profiles)):
            unknown = await tool.handler(context, {'member_ids': [41]})
        self.assertTrue(unknown['unknown_accounts'])
        for account in delivered(unknown)['unknown_accounts']:
            self.assertNotIn('roles', account)
