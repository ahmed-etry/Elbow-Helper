"""Necessary dependent research survives a large, complete retained cohort."""
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.conversation.context import estimate_tokens
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.answer_inputs import serialized_answer_evidence
from elbow_helper.agent.engine.budgets import ASSESSMENT_LIMITS
from elbow_helper.agent.families.core import family_tool
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from agent.engine.test_agent_plan_flow import _context, _model_step, _plan, _Session
from agent.engine.test_writer_tables import expand


class ResearchCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_source_is_read_before_composing_the_full_retained_cohort(self):
        await self._exercise(cutoff=False)

    async def test_full_evidence_and_missing_source_survive_a_reasoning_only_final(self):
        await self._exercise(cutoff=True)

    async def test_larger_complete_evidence_uses_the_measured_assessment_allowance(self):
        result = await self._exercise(cutoff=True, note_repeats=120,
            input_allowance=ASSESSMENT_LIMITS.input_tokens, continuation_reads=2)
        final = next(call['input'] for call in result['calls'] if call['kind'] == 'final')
        self.assertGreater(2 * final, 240000)
        self.assertLess(result['spent_input'], ASSESSMENT_LIMITS.input_tokens)

    async def test_reasoning_only_final_recovers_near_the_shared_input_boundary(self):
        measured = await self._exercise(cutoff=True)
        padding = measured['remaining_input'] - 12000
        self.assertGreater(padding, 0)
        near = await self._exercise(cutoff=True, initial_input=32000 + padding)
        # Opaque report IDs slightly vary the token estimate across the runs.
        self.assertLessEqual(near['remaining_input'], 12512)
        self.assertGreaterEqual(near['remaining_input'], 0)

    async def _exercise(self, *, cutoff, initial_input=32000, note_repeats=60,
                        input_allowance=240000, continuation_reads=1):
        # Keep the tighter former cap for the core regression: the research
        # fix must work independently of the measured large-evidence allowance.
        rows = [{'player_tag': f'#P{i}', 'player_name': f'Account {i}',
                 'notes': ('Observed activity retained for evaluation. ' * note_repeats) + str(i)}
                for i in range(120)]
        finding = 'These accounts are marked for maintenance.'
        read_accounts = AsyncMock(return_value={'rows': rows})

        async def discussion(context, arguments):
            cursor = arguments.get('source_cursor')
            stage = 0 if not cursor else 1 if cursor == 'earlier' else 2
            complete = stage == continuation_reads
            return {'rows': [{'message_id': 401 + stage, 'channel_id': 91,
                'author_id': 52, 'author': 'Reviewer', 'timestamp': '2026-01-01T00:00:00+00:00',
                'content': finding if complete else 'The purpose was agreed in an earlier discussion.',
                'content_complete': True}], 'mode': 'channel_window', 'channel_id': 91,
                'source_next_cursor': None if complete else 'earlier' if stage == 0 else 'earliest',
                'coverage': {'reached_requested_start': complete}}

        read_discussion = AsyncMock(side_effect=discussion)
        registry = {
            'read_member_accounts': family_tool('read_member_accounts', 'Read selected accounts.', {}, read_accounts),
            'read_conversation_evidence': family_tool('read_conversation_evidence', 'Read source discussion.',
                {'mode': {'type': 'string'}, 'channel_id': {'type': 'integer'},
                 'source_cursor': {'type': 'string'}}, read_discussion),
        }
        first = {**_plan([
            {'id': 'accounts', 'capability': 'read_member_accounts',
             'arguments': {'limit': 1}, 'depends_on': []},
            {'id': 'discussion', 'capability': 'read_conversation_evidence',
             'arguments': {'mode': 'channel_window', 'channel_id': 91}, 'depends_on': []}]),
             'assessment': True, 'discussion_channels': [91]}
        continuation = {**_plan([{'id': 'earlier', 'capability': 'read_conversation_evidence',
            'arguments': {'mode': 'channel_window', 'channel_id': 91, 'source_cursor': 'earlier'},
            'depends_on': []}]), 'assessment': True, 'discussion_channels': [91],
            'finish_after_reads': True}
        initial = _Session([replace(_model_step(first), usage=AgentUsage(initial_input, 1000))], [])
        requests, calls = [], []
        test = self

        class Session:
            context_window_tokens = 1_000_000

            def __init__(self, options):
                self.options = options

            async def advance(self, results=(), **options):
                request = self.options
                prompt = request['prompt']
                evidence = expand(json.loads(prompt.split('\nAvailable evidence:\n')[1]))['results']
                projected = estimate_tokens(request['system_prompt'] + prompt
                    + options.get('continuation_instruction', '')
                    + json.dumps([tool.parameters for tool in request['tools']])) + 512
                kind = 'review' if request['tools'] else 'final'
                calls.append({'kind': kind, 'input': projected,
                    'effort': options['reasoning_effort'], 'output_limit': options['max_output_tokens']})
                if kind == 'review':
                    test.assertTrue(options['allow_tools'])
                    test.assertNotIn('rows', evidence['accounts'])
                    test.assertEqual(evidence['accounts']['retained_table']['total_rows'], len(rows))
                    handles = json.loads(prompt.split('\nAvailable evidence:\n')[0])['read_handles']
                    review_number = sum(call['kind'] == 'review' for call in calls)
                    source = 'discussion' if review_number == 1 else 'earlier'
                    cursor = 'earlier' if review_number == 1 else 'earliest'
                    test.assertEqual(handles[source]['source_next_arguments']['source_cursor'], cursor)
                    test.assertEqual(read_discussion.await_count, review_number)
                    unbounded = json.loads(prompt.split('\nAvailable evidence:\n')[1])
                    unbounded['results']['accounts']['rows'] = rows
                    full_prompt = prompt.split('\nAvailable evidence:\n')[0] + '\nAvailable evidence:\n'
                    full_prompt += serialized_answer_evidence(unbounded['results'])
                    calls[-1]['full_table_input_equivalent'] = estimate_tokens(
                        request['system_prompt'] + full_prompt + options.get('continuation_instruction', '')
                        + json.dumps([tool.parameters for tool in request['tools']])) + 512
                    next_plan = {**continuation, 'steps': [{**continuation['steps'][0], 'id': cursor,
                        'arguments': {**continuation['steps'][0]['arguments'], 'source_cursor': cursor}}]}
                    return AgentStep('', (AgentToolCall('continue', 'submit_request_plan',
                        json.dumps(next_plan)),), AgentUsage(projected, 400))
                test.assertEqual(read_discussion.await_count, continuation_reads + 1,
                                 'A necessary source read was skipped.')
                test.assertFalse(options['allow_tools'])
                test.assertEqual(evidence['accounts']['rows'], rows)
                source = 'earlier' if continuation_reads == 1 else 'earliest'
                test.assertEqual(evidence[source]['rows'][0]['content'], finding)
                if cutoff and sum(call['kind'] == 'final' for call in calls) == 1:
                    return AgentStep('', (), AgentUsage(projected, options['max_output_tokens']),
                                     output_limit_reached=True)
                content = json.dumps({'intent': 'Evaluate the selected accounts with their documented purpose',
                    'intent_source': 'discussion', 'discussion_message_ids': [401 + continuation_reads],
                    'settled_choices': [], 'open_choices': [], 'facts': finding,
                    'question': '', 'recommendation': ''})
                return AgentStep(content, (), AgentUsage(projected, 1000))

        class Model:
            def create_agent_session(self, **options):
                requests.append(options)
                return initial if len(requests) == 1 else Session(options)

        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
            flow, _ = AgentService(Model())._start_flow(
                question='Review all selected accounts using their agreed purpose.', local_context='',
                context=_context(), conversation_history='')
        with patch('elbow_helper.agent.engine.budgets.ASSESSMENT_LIMITS',
                   replace(ASSESSMENT_LIMITS, input_tokens=input_allowance)):
            answer = await flow.run()
        self.assertEqual(answer, finding)
        self.assertEqual([call['kind'] for call in calls],
            ['review'] * continuation_reads + ['final'] + (['final'] if cutoff else []))
        self.assertEqual([call['effort'] for call in calls],
            [AgentReasoningEffort.HIGH] * continuation_reads + [AgentReasoningEffort.MAX]
            + ([AgentReasoningEffort.NONE] if cutoff else []))
        read_accounts.assert_awaited_once()
        self.assertLess(flow.budget.spent_input, flow.budget.request_limits.input_tokens)
        self.assertLess(flow.budget.spent_output, flow.budget.request_limits.output_tokens)
        final = next(call['input'] for call in calls if call['kind'] == 'final')
        self.assertTrue(all(call['input'] < final / 3 for call in calls if call['kind'] == 'review'))
        if cutoff:
            self.assertEqual(requests[-1]['prompt'], requests[-2]['prompt'])
        return {'calls': calls, 'spent_input': flow.budget.spent_input,
                'remaining_input': flow.budget.request_limits.input_tokens - flow.budget.spent_input,
                'full_final_input_estimate': flow.budget.final_input_estimate,
                'initial_input': initial_input, 'retained_rows': len(rows)}
