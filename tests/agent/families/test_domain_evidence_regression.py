"""Identity evidence carries policy unknowns without exposing testing roles."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID
from elbow_helper.agent.families import identity_reads
from agent.engine.test_agent_plan_flow import _context
from agent.families.test_compact_family_regressions import delivered


class DomainEvidenceRegressionTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertIn(AGENT_TESTER_ROLE_ID, [role['role_id'] for role in result['rows'][0]['roles']])
        profiles['#P0']['status'] = 'unavailable'
        with patch.object(identity_reads, 'profiles', AsyncMock(return_value=profiles)):
            unknown = await tool.handler(context, {'member_ids': [41]})
        self.assertTrue(unknown['unknown_accounts'])
        for account in delivered(unknown)['unknown_accounts']:
            self.assertNotIn(AGENT_TESTER_ROLE_ID, [role['role_id'] for role in account['roles']])
