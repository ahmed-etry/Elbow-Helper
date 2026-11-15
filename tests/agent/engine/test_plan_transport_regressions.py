"""Recorded failures at the boundary between a plan, its pages and the final answer."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.actions.contracts import ActionClass, ChangePreview, PreparedAction
from elbow_helper.agent.engine.budgets import ContextBudget
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.engine.models import AgentCapabilityEffect, RegisteredAgentTool
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.plan.checker import check_plan
from elbow_helper.agent.reads.evidence import record_rows
from elbow_helper.agent.reads.snapshots import family_tool
from elbow_helper.infrastructure.ai import AgentToolDefinition
from tests.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan
from tests.agent.engine.test_writer_tables import expand


def _step(identity, capability, arguments=None, depends_on=()):
    return {'id': identity, 'capability': capability, 'arguments': arguments or {},
            'depends_on': list(depends_on)}


def _flow(context, registry):
    with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
        flow, _ = AgentService(_Model(_Session([], [])))._start_flow(
            question='Compare the requested family records', local_context='',
            context=context, conversation_history='')
    return flow


class PlanTransportRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_coin_grant_reuses_discovery_name_without_losing_evidence_or_preview_identity(self):
        arguments = {'user': 1173678424297062513, 'category': 'encouragement', 'amount': 50,
                     'reason': 'Top donator in BEH this month (3,820 donations, Nov 1-15)'}
        apply = AsyncMock()

        async def prepare(context, values):
            context.state.proposed_changes.append(PreparedAction(
                '/grant coins', dict(values),
                ChangePreview(('Grant the requested coins',), AsyncMock(return_value=True)), apply))
            return {'status': 'confirmation_required'}

        grant = RegisteredAgentTool(AgentToolDefinition('grant_coins', 'Prepare a coin grant.', {
            'type': 'object', 'properties': {
                'user': {'type': 'integer'}, 'category': {'type': 'string'},
                'amount': {'type': 'integer'}, 'reason': {'type': 'string'}}}),
            prepare, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(), default_visible=False, specialist_group='achievements')
        first = _plan([_step('coins', 'open_capability_group', {
            'group': 'records', 'capabilities': ['grant_coins'], 'intent': 'prepare_change'})])
        revision = _plan([_step('coins', 'grant_coins', arguments)])
        session = _Session([_model_step(first), _model_step(revision)], [])
        session.replace_tools = Mock()
        context = replace(_context(), bot=SimpleNamespace(tree=object()))
        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value={'grant_coins': grant}), \
                patch('elbow_helper.agent.engine.service.build_command_tools', return_value=({}, {})):
            answer = await AgentService(_Model(session)).answer(
                question='Give 50 coins to the top BEH donator', local_context='', context=context)
        self.assertIn('Grant the requested coins', answer)
        proposal, = context.state.proposed_changes
        self.assertEqual(proposal.step_id, 'coins_2')
        self.assertEqual(proposal.checked_arguments, arguments)
        apply.assert_not_awaited()
        records = [json.loads(encoded) for encoded in context.state.evidence]
        self.assertEqual([record['call_id'] for record in records], ['coins', 'coins_2'])
        self.assertEqual(len(session.calls), 2)

        # References, ordering, bare report names and explicitly chosen suffixes
        # all address the new operation without taking any earlier result's name.
        source = family_tool('read_member_accounts', 'Read accounts.', {}, AsyncMock())
        consumer = replace(family_tool('consume', 'Read a report.', {
            'cohort_ref': {'type': 'string'}, 'nested': {'type': 'array', 'items': {'type': 'integer'}}},
            AsyncMock()), contract=CapabilityContract(entity_fields=(('cohort_ref', 'family_report'),)))
        steps = [_step('coins', 'read_member_accounts'), _step('coins_2', 'read_member_accounts'),
                 _step('use', 'consume', {'cohort_ref': 'coins',
                     'nested': [{'step': 'coins', 'path': ['rows', '*', 'member_id']}]}, ['coins'])]
        checked = _plan(deepcopy(steps))
        check = check_plan(checked, {'read_member_accounts': source, 'consume': consumer,
                           'open_capability_group': source}, completed_steps={'coins': first['steps'][0]})
        self.assertTrue(check.ok, check.error)
        self.assertFalse(check.step_errors)
        self.assertEqual(checked['steps'][0]['id'], 'coins_3')
        self.assertEqual(checked['steps'][-1]['depends_on'], ['coins_3'])
        self.assertEqual(checked['steps'][-1]['arguments']['cohort_ref']['step'], 'coins_3')
        self.assertEqual(checked['steps'][-1]['arguments']['nested'][0]['step'], 'coins_3')

    async def test_evicted_participation_handle_is_replaced_by_source_arguments_in_planning(self):
        context = _context()
        source = AsyncMock(return_value={'rows': [{'player_tag': '#P0', 'stars': 12}] * 59})
        participation = family_tool('read_war_participation', 'Read participation.', {'after': {'type': 'string'}}, source)
        rosters = family_tool('read_clan_rosters', 'Read rosters.', {'clan_codes': {'type': 'array',
            'items': {'type': 'string'}}}, source)
        flow = _flow(context, {'read_war_participation': participation, 'read_clan_rosters': rosters})
        steps = [_step('rosters', 'read_clan_rosters'), _step('participation', 'read_war_participation',
            {'after': '2026-10-15T00:00:00+00:00', 'limit': 27})]
        steps += [_step('clan' + str(i), 'read_clan_rosters', {'clan_codes': [code]})
                  for i, code in enumerate(('BE1', 'BEM', 'BEP', 'BEW', 'BES', 'BEH'))]
        for step in steps:
            results = await flow.runner.run(_plan([step]))
            flow.completed_results.update(results)
            flow.result_views.update(flow.runner.model_results_for(results))
            flow.steps_by_id[step['id']] = step
        expired = flow.completed_results['participation']['report_id']
        self.assertEqual(len(context.state.reports), 6)
        self.assertNotIn(expired, context.state.reports)
        visible = flow.model_results(flow.completed_results)
        frame = json.loads(flow._answer_frame(visible, allow_more=True, instruction=''))
        self.assertNotIn(expired, json.dumps(frame))
        self.assertEqual(frame['read_handles']['participation']['source_next_arguments']['after'],
                         '2026-10-15T00:00:00+00:00')
        self.assertNotIn('report_id', visible['participation'])
        self.assertNotIn('next_cursor', visible['participation'])
        self.assertNotIn('next_offset', visible['participation'])
        self.assertFalse(visible['participation']['complete_snapshot'])
        self.assertEqual(visible['participation']['total_rows'], 59)
        self.assertEqual(len(record_rows(visible['participation']['rows'])), 27)
        held = flow.completed_results['clan5']['report_id']
        self.assertEqual(frame['read_handles']['clan5']['report_id'], held)

    async def test_eight_successful_reports_reach_writer_when_late_planner_pages_have_no_room(self):
        context = _context()
        fields = {'cohort_ref': {'type': 'string'}, 'war_type': {'type': 'string'}}
        registry = {}
        for name in ('read_member_accounts', 'read_member_cohort', 'read_war_participation', 'read_ranked_activity'):
            source = AsyncMock(return_value={'rows': [{'player_tag': '#P0', 'stars': 18,
                'attacks_used': 6, 'attacks_expected': 7}]})
            registry[name] = family_tool(name, 'Read the selected records.', fields, source)
        flow = _flow(context, registry)
        steps = [_step('asker', 'read_member_accounts'), _step('coleads', 'read_member_cohort')]
        for suffix, capability, arguments in (
                ('reg', 'read_war_participation', {'war_type': 'REG'}),
                ('cwl', 'read_war_participation', {'war_type': 'CWL'}),
                ('ranked', 'read_ranked_activity', {})):
            for who in ('asker', 'coleads'):
                steps.append(_step(who + '_' + suffix, capability, {**arguments,
                    'cohort_ref': {'step': who, 'path': ['report_id']}}, [who]))
        # The real run exhausted the shared page allowance after its first four
        # reads: later pages either had no room for a row or only an empty shell.
        with patch.object(ContextBudget, 'result_character_limit', side_effect=[16000] * 4 + [1, 300, 1, 300]):
            results = await flow.runner.run(_plan(steps))
        self.assertEqual(len(results), 8)
        self.assertTrue(all('error' not in value for value in results.values()))
        flow.completed_results.update(results)
        flow.result_views.update(flow.runner.model_results_for(results))
        flow.steps_by_id.update({step['id']: step for step in steps})
        requests = []
        flow.final_session_factory = lambda **kwargs: (requests.append(kwargs) or _Session([], []))
        self.assertTrue(await flow._prepare_answer_session(allow_more=True))
        frame = json.loads(requests[-1]['prompt'].split('\nAvailable evidence:\n')[0])
        self.assertIn('report_id', frame['read_handles']['coleads_ranked'])
        self.assertEqual(frame['read_handles']['asker_cwl']['next_offset'], 0)
        self.assertEqual(frame['read_handles']['asker_cwl']['report_id'], results['asker_cwl']['report_id'])
        self.assertTrue(await flow._prepare_answer_session())
        evidence = expand(json.loads(requests[-1]['prompt'].split('\nAvailable evidence:\n')[1]))['results']
        self.assertEqual(set(evidence) - {'channel_directory'}, set(results))
        for identity in results:
            self.assertNotIn('failed_read', evidence[identity])
            self.assertEqual(len(record_rows(evidence[identity]['rows'])), 1)
            self.assertEqual(record_rows(evidence[identity]['rows'])[0]['player_tag'], '#P0')
            if steps[list(results).index(identity)]['capability'] == 'read_war_participation':
                self.assertEqual(record_rows(evidence[identity]['rows'])[0]['stars'], 18)
