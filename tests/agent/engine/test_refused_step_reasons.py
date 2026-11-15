"""A step refused at plan check keeps its reason in what the next planning round and the writer read."""

import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.models import RegisteredAgentTool
from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.engine.routing import ROUTE_INSTRUCTION
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.reads.identity_reads import TOOLS
from elbow_helper.infrastructure.ai import AgentToolDefinition
from tests.agent.engine.test_agent_plan_flow import _context, _plan
from tests.agent.engine.test_answer_composition import call, client, provider_response, writer_payload

REFUSAL = 'look_in is required for name lookups; choose game or discord.'


def _registry():
    registry = {tool.definition.name: tool for tool in TOOLS if tool.definition.name == 'read_member_accounts'}
    registry['read_cwl_results'] = RegisteredAgentTool(AgentToolDefinition(
        'read_cwl_results', 'Synthetic CWL read.', {'type': 'object', 'properties': {
            'metric': {'type': 'string'}, 'account_tags': {'type': 'array', 'items': {'type': 'string'}}}}),
        AsyncMock(return_value={'rows': []}))
    return registry


def _refused_plan():
    return _plan([
        {'id': 'noxxy', 'capability': 'read_member_accounts', 'arguments': {'name': 'noxxy'}, 'depends_on': []},
        {'id': 'cwl', 'capability': 'read_cwl_results', 'depends_on': ['noxxy'], 'arguments': {
            'metric': 'performance',
            'account_tags': {'step': 'noxxy', 'path': ['rows', '*', 'player_tag']}}}])


class RefusedStepReasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_skipped_step_keeps_why_it_did_not_run_without_source_values(self):
        skipped = {'error': 'A required earlier result is unavailable.', 'execution_status': 'not_run'}
        projected = answer_evidence({'msgs': skipped})['msgs']
        self.assertEqual(projected, {'status': 'not_checked', 'refused': {
            'what': 'msgs', 'reason': 'A required earlier result is unavailable.'}})
        denied = answer_evidence({'msgs': {**skipped, 'cause': 'access_denied',
                                          'error': 'restricted-source'}})['msgs']
        self.assertEqual(denied, {'status': 'denied', 'refused': {
            'what': 'msgs', 'reason': 'access_denied'}})

    async def test_refusal_reaches_the_next_planning_round_and_the_writer(self):
        registry = _registry()
        seen = []

        async def provider(**options):
            system = options['messages'][0]['content']
            if system == ROUTE_INSTRUCTION:
                return provider_response('research')
            seen.append(options)
            if len(seen) < 3:
                return provider_response(calls=[call('submit_request_plan', _refused_plan())])
            return provider_response('That was not checked.')

        model = client(provider)
        try:
            with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
                await AgentService(model).answer(question='how is noxxy doing in cwl so far',
                                                 local_context='', context=_context())
        finally:
            await model.close()
        self.assertEqual(len(seen), 3)
        for options in seen[1:]:
            evidence = writer_payload(options)[2]
            self.assertEqual(evidence['noxxy']['status'], 'not_checked')
            self.assertEqual(evidence['noxxy']['refused'], {
                'what': 'read_member_accounts', 'reason': REFUSAL, 'missing_fields': ['look_in']})
            self.assertEqual(evidence['cwl']['refused'], {
                'what': 'read_cwl_results', 'reason': 'Step noxxy failed, so this step could not run.'})
