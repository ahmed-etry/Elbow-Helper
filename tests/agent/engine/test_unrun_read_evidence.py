"""Skipped execution reaches the isolated writer as unchecked evidence."""

import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.steps import PlanRunner
from elbow_helper.agent.engine.routing import ROUTE_INSTRUCTION
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.agent.plan.format import answer_instructions
from elbow_helper.infrastructure.ai import AgentToolDefinition
from agent.engine.test_agent_plan_flow import _context, _plan
from agent.engine.test_answer_composition import call, client, provider_response, writer_payload


class UnrunReadEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_writer_distinguishes_skipped_dependencies_validation_and_budget(self):
        for mode in ('dependency', 'reference', 'validation', 'reservation'):
            with self.subTest(mode=mode):
                source = AsyncMock(return_value={'error': 'Synthetic transport failure'}
                    if mode == 'dependency' else {'value': 'invalid' if mode == 'validation' else 42})
                measure = AsyncMock(return_value={'rows': []})
                registry = {name: RegisteredAgentTool(AgentToolDefinition(name, 'Synthetic read.', {
                    'type': 'object', 'properties': {'member_id': {'type': 'integer'}}}), handler)
                    for name, handler in (('read_source', source), ('read_measure', measure))}
                arguments = {} if mode == 'reservation' else {'member_id': {
                    'step': 'source', 'path': ['missing' if mode == 'reference' else 'value']}}
                writers = []

                async def provider(**options):
                    system = options['messages'][0]['content']
                    if system == ROUTE_INSTRUCTION:
                        return provider_response('research')
                    if system == answer_instructions(registry):
                        writers.append(options)
                        _, _, evidence = writer_payload(options)
                        self.assertEqual(evidence['measure']['status'], 'not_checked')
                        self.assertNotIn('failed_read', evidence['measure'])
                        if mode == 'dependency':
                            self.assertEqual(evidence['source']['status'], 'unavailable')
                            self.assertEqual(evidence['source']['failed_read'], {
                                'what': 'read_source', 'reason': 'Synthetic transport failure'})
                        self.assertIn('could not be read for the stated reason, never that no data exists', system)
                        self.assertIn('planned read never ran: say not checked', system)
                        return provider_response('The requested activity has not been checked.')
                    return provider_response(calls=[call('submit_request_plan', _plan([
                        {'id': 'source', 'capability': 'read_source', 'arguments': {}, 'depends_on': []},
                        {'id': 'measure', 'capability': 'read_measure', 'arguments': arguments,
                         'depends_on': ['source']}]))])

                model = client(provider)
                reserve = PlanRunner._reserve_tool

                async def reserve_tool(runner, step, arguments, tool, state):
                    if mode == 'reservation' and step['id'] == 'measure':
                        return {'error': 'Synthetic reservation limit'}
                    return await reserve(runner, step, arguments, tool, state)

                try:
                    with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry), \
                         patch.object(PlanRunner, '_reserve_tool', reserve_tool):
                        answer = await AgentService(model).answer(question='Check synthetic activity.',
                            local_context='', context=_context())
                    self.assertEqual(answer, 'The requested activity has not been checked.')
                    self.assertEqual(len(writers), 1)
                    source.assert_awaited_once()
                    measure.assert_not_awaited()
                finally:
                    await model.close()
