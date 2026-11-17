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
    async def test_army_field_is_described_before_the_specialist_is_opened(self):
        from elbow_helper.agent.engine.registry import build_agent_tools

        class Client:
            configured = True

            async def get(self, *args, **kwargs):
                return SimpleNamespace(ok=True, status=200, payload={'items': [{
                    'armyShareCode': 'h0p9e14_8-1p3e17_20-2m1p0e34_4i1x0-2x57-1x51d1x98u1x150-6x147-2x1-1x52s3x10-2x109', 'attack': True,
                    'battleTimestamp': '20261115T120000.000Z'}]})

        context = replace(_context(), bot=SimpleNamespace(clash_client=Client()))
        model = _Model(_Session([], []))
        service = AgentService(model)
        service._start_flow(question='Which armies have I used in the past month?',
                            local_context='', context=context, conversation_history='')
        self.assertNotIn('read_clash:', model.request['system_prompt'])
        self.assertIn('read_clash kind=battle_log', model.request['system_prompt'])
        self.assertIn('items.armyShareCode', model.request['system_prompt'])
        result = await build_agent_tools()['read_clash'].handler(context, {
            'kind': 'battle_log', 'tags': ['#PYPYPY'], 'fields': ['items.armyShareCode']})
        self.assertEqual(result['items'][0]['data']['items'], [{'armyShareCode': {
            'heroes': {
                'Barbarian King': {'count': 1, 'pets': {'Frosty': 1},
                                  'equipment': {'Spiky Ball': 1, 'Earthquake Boots': 1}},
                'Archer Queen': {'count': 1, 'pets': {'Unicorn': 1},
                                 'equipment': {'Giant Arrow': 1, 'Healer Puppet': 1}},
                'Grand Warden': {'count': 1, 'm': 1, 'pets': {'L.A.S.S.I': 1},
                                 'equipment': {'Healing Tome': 1, 'Eternal Tome': 1}}},
            'clan_castle': {'troops': {'Barbarian': 1, 'Rocket Balloon': 2},
                            'siege_machines': {'Wall Wrecker': 1},
                            'spells': {'Revive Spell': 1}},
            'troops': {'Furnace': 1, 'Super Yeti': 6, 'Archer': 2},
            'siege_machines': {'Battle Blimp': 1},
            'spells': {'Earthquake Spell': 3, 'Ice Block Spell': 2}}}])

    async def test_full_three_account_battle_read_can_plan_for_omitted_codes(self):
        tags = ['#PYPYPY', '#QYQYQY', '#LPLPLP']
        arguments = {'kind': 'battle_log', 'tags': tags, 'detail': 'full', 'limit': 50}
        rows = [{'battleTimestamp': f'20261115T12{i:02d}00.000Z', 'attack': True,
                 'opponentName': 'Sample Rival',
                 'armyShareCode': f'h0p9e14_8-1p3e17_20u{i+1}x150-6x147-2x1s3x10-2x109',
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
        selected = {**recovery['arguments'], 'fields': [
            'items.armyShareCode', 'items.battleTimestamp', 'items.attack']}
        projected = await flow.runner.run(_plan([
            {'id': 'codes', 'capability': recovery['capability'], 'arguments': selected, 'depends_on': []},
        ]))
        planner = unpack(flow.runner.model_results_for(projected))['codes']
        writer = unpack(answer_evidence({'codes': planner}, guild=context.guild))['codes']
        for raw_item, plan_item, writer_item in zip(projected['codes']['items'], planner['items'], writer['items']):
            battles = raw_item['data']['items']
            self.assertEqual(len(battles), 50)
            self.assertEqual([row['armyShareCode']['troops']['Furnace'] for row in battles],
                             list(range(50, 0, -1)))
            shown = plan_item['data']['items']
            self.assertTrue(shown)
            self.assertEqual(shown, battles[:len(shown)])
            self.assertEqual(writer_item['data']['items'], shown)
            self.assertEqual(plan_item['coverage']['next_offset'], len(shown))
            self.assertEqual(plan_item['coverage']['returned_rows'], len(shown))
            for battle in shown:
                self.assertTrue(battle['attack'])
                self.assertIn('battleTimestamp', battle)
                self.assertEqual(battle['armyShareCode']['troops']['Super Yeti'], 6)
                self.assertEqual(battle['armyShareCode']['heroes']['Archer Queen']['pets'], {'Unicorn': 1})
        writer = answer_evidence({'battles': visible}, guild=context.guild)
        self.assertEqual(writer['battles']['omitted_field_reads'], visible['omitted_field_reads'])
        catalogue = capability_list(registry)
        self.assertIn('full may summarize to fit', catalogue)
        self.assertIn('fields selects paths over detail', catalogue)
