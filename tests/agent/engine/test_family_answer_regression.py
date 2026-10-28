"""Family reads must reach a compact answer-model session without prose formatting."""

from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.agent.engine.answer_inputs import answer_evidence, bounded_answer_evidence
from elbow_helper.agent.plan.results import model_result
from elbow_helper.agent.families.core import family_tool
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentStep, AgentToolDefinition, AgentUsage
from agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan
from agent.engine.test_writer_tables import expand


class FamilyAnswerRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_writer_receives_complete_typed_rows_without_failed_source_values(self):
        context = _context()
        rows = [{'member_id': 41, 'player_tag': '#P' + character,
                 'player_name': 'Account\n' + character if index < 11 else None,
                 'profile_status': 'ok' if index < 11 else 'unavailable'}
                for index, character in enumerate('0289PYLQGRJC')]

        async def source(context, arguments):
            return {'rows': rows, 'coverage': {'historical_collection_complete': False}}

        tool = family_tool('read_member_accounts', 'Read account identities.', {}, source)
        first = await tool.handler(context, {'limit': 1})
        evidence = await cohort_writer_evidence(context, {
            'failed': model_result({'error': 'Source unavailable.', 'cause': 'source_unavailable',
                                   'rows': [{'player_tag': '#Y0', 'player_name': 'Unverified'}]}),
            'good': model_result(first), 'repeat': model_result(first)})
        written = json.loads(bounded_answer_evidence(answer_evidence(evidence), 20_000))['results']
        for identity in ('good', 'repeat'):
            records = written[identity]['rows']
            decoded = expand(records)
            self.assertEqual(decoded, rows)
            self.assertFalse(written[identity]['coverage']['historical_collection_complete'])
        self.assertNotIn('rows', written['failed'])
        self.assertEqual(written['failed']['status'], 'unavailable')

    async def test_answer_model_writes_directory_from_compact_report_without_catalogue(self):
        async def source(context, arguments):
            return {'rows': [], 'clans': [{'clan_code': 'BEH', 'name': 'Current name',
                'tag': '#P0', 'in_game_members': 49, 'open_in_game_slots': 1}]}
        tool = family_tool('read_clan_rosters', 'Read directory facts.', {}, source)
        hidden = RegisteredAgentTool(AgentToolDefinition('hidden', 'Hidden specialist.',
            {'type': 'object', 'properties': {}}), source, default_visible=False)
        initial = _Session([replace(_model_step(_plan([{'id': 'directory',
            'capability': 'read_clan_rosters', 'arguments': {'view': 'count'}, 'depends_on': []}])),
            usage=AgentUsage(1000, 100))], [])
        expected = 'BEH — Current name — #P0 — 49 members — 1 free'
        final = _Session([AgentStep(expected, (), AgentUsage(1000, 100))], [])

        class Model(_Model):
            def create_agent_session(self, **kwargs):
                if not kwargs['tools']:
                    self.final_request = kwargs
                    return final
                return super().create_agent_session(**kwargs)

        model = Model(initial)
        with patch('elbow_helper.agent.engine.service.build_agent_tools',
                   return_value={'read_clan_rosters': tool, 'hidden': hidden}):
            flow, _ = AgentService(model)._start_flow(question='Give a compact clan directory',
                local_context='', context=_context(), conversation_history='')
        # Exhaust research room while leaving the ordinary final-answer allowance.
        policy = flow.budget.request_limits
        flow.budget.spent_rounds = policy.rounds - 2 - policy.answer_repair_rounds
        answer = await flow.run()
        self.assertEqual(answer, expected)
        self.assertEqual(len(final.calls), 1)
        evidence = json.loads(model.final_request['prompt'].split('\nAvailable evidence:\n', 1)[1])
        self.assertEqual(evidence['results']['directory']['clans'][0]['in_game_members'], 49)
        self.assertNotIn('<capabilities>', model.final_request['system_prompt'])
        self.assertEqual(model.final_request['tools'], ())
        self.assertFalse(flow.context.state.attachments)
