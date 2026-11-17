"""An oversized full battle read leaves a usable route to its omitted fields."""

import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.plan.checker import check_plan
from elbow_helper.agent.plan.format import capability_list
from elbow_helper.agent.reads.clash import clash_tools
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from tests.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan
from tests.agent.engine.test_writer_delivery_evidence import unpack


class ClashOmissionPlanningTests(unittest.IsolatedAsyncioTestCase):

    async def test_full_three_account_battle_read_can_plan_for_omitted_codes(self):
        tags = ['#PYPYPY', '#QYQYQY', '#LPLPLP']
        arguments = {'kind': 'battle_log', 'tags': tags, 'detail': 'full', 'limit': 50}
        rows = [{'battleTimestamp': f'20261115T12{i:02d}00.000Z', 'attack': True,
                 'opponentName': 'Sample Rival', 'armyShareCode': 'sample-army-' + str(i) + 'x' * 60,
                 'availableLoot': [{'name': 'gold', 'amount': 100000}]} for i in range(50)]

        class Client:
            configured = True

            async def get(self, *args, **kwargs):
                return SimpleNamespace(ok=True, payload={'items': rows}, status=200)

        context = replace(_context(), bot=SimpleNamespace(clash_client=Client()))
        tool = clash_tools()[0]
        registry = {'read_clash': tool}
        plan = {**_plan([{'id': 'battles', 'capability': 'read_clash',
                         'arguments': arguments, 'depends_on': []}]), 'finish_after_reads': True}
        session = _Session([_model_step(plan), AgentStep('Supported sample answer.', (), AgentUsage())], [])
        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
            flow, _ = AgentService(_Model(session))._start_flow(question='Which armies did these accounts use?',
                local_context='', context=context, conversation_history='')
        self.assertEqual(await flow.run(), 'Supported sample answer.')
        # The continuation is eligible to submit a plan, rather than forced to write.
        self.assertTrue(session.calls[1][1])
        visible = unpack(json.loads(session.calls[1][0][0].content))['results']['battles']
        self.assertTrue(visible['truncated'])
        recovery = visible['omitted_field_reads'][0]
        self.assertEqual(recovery['capability'], 'read_clash')
        self.assertEqual(recovery['arguments']['tags'], tags)
        self.assertEqual(recovery['arguments']['limit'], 50)
        self.assertIn('items.armyShareCode', recovery['arguments']['fields'])
        followup = _plan([{'id': 'codes', **recovery, 'depends_on': []}])
        self.assertTrue(check_plan(followup, registry).ok)
        selected = {**recovery['arguments'], 'fields': ['items.armyShareCode']}
        projected = await flow.runner.run(_plan([
            {'id': 'codes', 'capability': recovery['capability'], 'arguments': selected, 'depends_on': []},
        ]))
        planner = unpack(flow.runner.model_results_for(projected))['codes']
        writer = unpack(answer_evidence({'codes': planner}, guild=context.guild))['codes']
        for delivered in (planner, writer):
            self.assertEqual(sum(len(item['data']['items']) for item in delivered['items']), 150)
            self.assertTrue(all(row['armyShareCode'] for item in delivered['items']
                                for row in item['data']['items']))
        writer = answer_evidence({'battles': visible}, guild=context.guild)
        self.assertEqual(writer['battles']['omitted_field_reads'], visible['omitted_field_reads'])
        catalogue = capability_list(registry)
        self.assertIn('full may summarize to fit', catalogue)
        self.assertIn('fields selects paths over detail', catalogue)
