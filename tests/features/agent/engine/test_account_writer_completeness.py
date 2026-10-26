"""Account pages preserve every linked identity while sharing owner roles once."""
import json
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.features.agent.engine.answer_inputs import answer_evidence, bounded_answer_evidence
from elbow_helper.features.agent.families.core import family_tool
from elbow_helper.features.agent.plan.results import model_result
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.engine.test_planner_fix2_evidence import unpack


class AccountWriterCompletenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_account_snapshot_replaces_first_page_without_repeating_roles(self):
        context = _context()
        roles = [{'role_id': i + 1000, 'name': f'Community role {i}', 'clan_purposes': []} for i in range(70)]
        rows = [{'member_id': 41, 'member_name': 'Owner', 'player_tag': f'#P{i}',
                 'player_name': f'Alt mark {i}', 'profile_status': 'ok' if i < 4 else 'unavailable',
                 'roles': roles, 'home_clans': [{'clan_code': 'BEM'}]} for i in range(5)]
        tool = family_tool('read_member_accounts', 'Read owner accounts.',
            {'include_roles': {'type': 'boolean'}}, AsyncMock(return_value={'rows': rows}))
        page = await tool.handler(context, {'include_roles': True, 'limit': 1})
        self.assertEqual(len(page['rows']), 1)
        evidence = await cohort_writer_evidence(context, {'accounts': model_result(page)})
        projected = answer_evidence(evidence, guild=context.guild)
        written = unpack(json.loads(bounded_answer_evidence(projected, 20_000)))['results']['accounts']
        self.assertEqual([r['player_tag'] for r in written['rows']], [r['player_tag'] for r in rows])
        self.assertEqual(written['rows'][-1]['profile_status'], 'unavailable')
        self.assertEqual(written['members'], [{'member_id': 41, 'member_name': 'Owner', 'roles': roles}])
        self.assertTrue(all('roles' not in row for row in written['rows']))
        self.assertTrue(all(row['home_clans'] == [{'clan_code': 'BEM'}] for row in written['rows']))
        self.assertEqual(json.loads(context.state.reports[page['report_id']].payload)['rows'], rows)
        context.state.report_sources.clear()
        denied = await cohort_writer_evidence(context, {'accounts': model_result(page)})
        self.assertNotIn('rows', denied['accounts'])

    async def test_roles_stay_hidden_when_not_requested(self):
        context = _context()
        tool = family_tool('read_member_accounts', 'Read owner accounts.', {}, AsyncMock(return_value={
            'rows': [{'member_id': 41, 'player_tag': '#P0', 'roles': [{'role_id': 7, 'name': 'Private'}]}]}))
        page = await tool.handler(context, {})
        written = unpack(await cohort_writer_evidence(context, {'accounts': model_result(page)}))['accounts']
        self.assertNotIn('members', written)
        self.assertNotIn('roles', written['rows'][0])
