"""Restored evidence chooses the final protocol before the provider call."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.engine.budgets import ASSESSMENT_LIMITS
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from features.agent.engine import test_request_cost as cost
from features.agent.engine.test_writer_tables import expand
from elbow_helper.features.agent.conversation.context import estimate_tokens
from elbow_helper.features.agent.engine.rounds import AgentLimitReached


class ResearchHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_cutoff_visible_assessment_is_preserved_as_a_draft_in_fresh_recovery(self):
        flow, _, _, _ = cost.RequestCostTests().flow([])
        flow.assessment_requested = True
        flow.budget.request_limits = ASSESSMENT_LIMITS
        draft = '{"intent":"Expand the casual group","settled_choices":["Keep the existing team together"],"facts":"'
        content = json.dumps({'intent': 'Expand the casual group', 'intent_source': 'request',
            'discussion_message_ids': [], 'settled_choices': ['Keep the existing team together'],
            'open_choices': [], 'facts': 'Both selected accounts.', 'question': '',
            'recommendation': 'Invite willing members.'})
        rows = [{'player_tag': '#P0'}, {'player_tag': '#P1'}]
        flow.completed_results = {'accounts': {'rows': rows}}
        flow.result_views = dict(flow.completed_results)
        flow.rounder.partial_content = 'Stale planning fragment'
        sessions, inputs = [], []

        def factory(**kwargs):
            step = (AgentStep(draft, (), AgentUsage(5000, 49152), output_limit_reached=True)
                    if not sessions else AgentStep(content, (), AgentUsage(6000, 1000)))
            session = cost.Session([step], [])
            sessions.append(session)
            inputs.append(kwargs)
            return session

        flow.final_session_factory = factory
        self.assertEqual(await flow._compact_reply(), content)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(inputs[0]['prompt'], inputs[1]['prompt'])
        evidence = json.loads(inputs[1]['prompt'].split('\nAvailable evidence:\n')[1])
        self.assertEqual(expand(evidence)['results']['accounts']['rows'], rows)
        instruction = sessions[1].instructions[0]
        self.assertIn(json.dumps(draft, ensure_ascii=False), instruction)
        self.assertNotIn('Stale planning fragment', instruction)
        self.assertIn('verify its claims', instruction)
        self.assertEqual(sessions[0].calls[0][2], AgentReasoningEffort.MAX)
        self.assertEqual(sessions[1].calls[0][2], AgentReasoningEffort.NONE)
        self.assertLess(flow.budget.spent_input, ASSESSMENT_LIMITS.input_tokens)

    async def test_late_review_pressure_never_bills_a_short_answer_with_the_review_protocol(self):
        flow, session, _, _ = cost.RequestCostTests().flow([])
        budget = flow.budget
        budget.request_limits = ASSESSMENT_LIMITS
        budget.final_input_estimate = 80000
        budget.next_input_estimate = 20000
        budget.spent_output = 2000
        budget.spent_input = ASSESSMENT_LIMITS.input_tokens - 20000 - 2 * 80000 - 8192
        self.assertTrue(budget.request_can_research(20000))
        with self.assertRaises(AgentLimitReached):
            await flow.rounder.advance(allow_tools=True,
                continuation_instruction='Additional required delivery context. ' * 20)
        self.assertEqual(session.calls, [])
        self.assertEqual(budget.spent_rounds, 0)

    async def test_full_input_recovery_is_admitted_only_when_the_complete_prefix_fits_again(self):
        for extra, succeeds in ((-512, True), (1024, False)):
            with self.subTest(extra_input=extra):
                flow, _, _, _ = cost.RequestCostTests().flow([])
                flow.assessment_requested = True
                flow.budget.request_limits = ASSESSMENT_LIMITS
                flow.budget.spent_output = 2000
                rows = [{'player_tag': str(i), 'observations': 'Selected evidence ' + str(i)}
                        for i in range(120)]
                flow.completed_results = {'accounts': {'rows': rows}}
                flow.result_views = dict(flow.completed_results)
                calls = []
                input_size = None
                content = json.dumps({'intent': 'Evaluate selected accounts', 'intent_source': 'request',
                    'discussion_message_ids': [], 'settled_choices': [], 'open_choices': [],
                    'facts': 'Complete selected table.', 'question': '', 'recommendation': ''})
                test = self

                class Session:
                    context_window_tokens = 1_000_000

                    def __init__(self, request):
                        self.request = request

                    async def advance(self, results=(), **options):
                        evidence = json.loads(self.request['prompt'].split('\nAvailable evidence:\n')[1])
                        test.assertEqual(expand(evidence)['results']['accounts']['rows'], rows)
                        calls.append(options)
                        if len(calls) == 1:
                            return AgentStep('', (), AgentUsage(input_size, 49152), output_limit_reached=True)
                        return AgentStep(content, (), AgentUsage(input_size, 1000))

                flow.final_session_factory = lambda **request: Session(request)
                self.assertTrue(await flow._prepare_answer_session())
                input_size = flow.budget.final_input_estimate
                flow.budget.spent_input = ASSESSMENT_LIMITS.input_tokens - 2 * input_size + extra
                answer = await flow._compact_reply()
                self.assertEqual(answer, content if succeeds else None)
                self.assertEqual(len(calls), 2 if succeeds else 1)
                self.assertLessEqual(flow.budget.spent_input, ASSESSMENT_LIMITS.input_tokens)

    async def test_reasoning_only_final_recovers_with_the_same_complete_evidence_in_a_fresh_session(self):
        content = json.dumps({'intent': 'Choose the supported next step',
            'intent_source': 'request', 'discussion_message_ids': [],
            'settled_choices': [], 'open_choices': [], 'facts': 'Complete selected account table.',
            'question': '', 'recommendation': ''})
        flow, _, _, _ = cost.RequestCostTests().flow([])
        flow.assessment_requested = True
        flow.budget.request_limits = ASSESSMENT_LIMITS
        flow.budget.spent_input = 32000
        flow.budget.spent_output = 2000
        rows = [{'player_tag': str(i), 'activity': {'used': i, 'expected': i + 1}}
                for i in range(120)]
        flow.completed_results = {'accounts': {'rows': rows}}
        flow.result_views = dict(flow.completed_results)
        sessions, inputs = [], []

        def factory(**kwargs):
            projected = estimate_tokens(kwargs['system_prompt'] + kwargs['prompt']) + 2000
            step = (AgentStep('', (), AgentUsage(projected, 49152), output_limit_reached=True)
                    if not sessions else AgentStep(content, (), AgentUsage(projected, 1500)))
            session = cost.Session([step], [])
            sessions.append(session)
            inputs.append(kwargs)
            return session

        flow.final_session_factory = factory
        self.assertEqual(await flow._compact_reply(), content)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(inputs[0]['prompt'], inputs[1]['prompt'])
        evidence = json.loads(inputs[1]['prompt'].split('\nAvailable evidence:\n')[1])
        self.assertEqual(expand(evidence)['results']['accounts']['rows'], rows)
        self.assertEqual(sessions[0].calls[0][2], AgentReasoningEffort.MAX)
        self.assertEqual(sessions[1].calls[0][2], AgentReasoningEffort.NONE)
        self.assertEqual(sessions[0].output_limits, [49152])
        self.assertGreaterEqual(sessions[1].output_limits[0], 8192)
        self.assertLess(flow.budget.spent_input, ASSESSMENT_LIMITS.input_tokens)
        self.assertLess(flow.budget.spent_output, ASSESSMENT_LIMITS.output_tokens)

    def test_research_reserves_the_measured_final_and_recovery_in_the_existing_allowance(self):
        flow, _, _, _ = cost.RequestCostTests().flow([])
        budget = flow.budget
        budget.request_limits = ASSESSMENT_LIMITS
        budget.spent_input = ASSESSMENT_LIMITS.input_tokens - 20000 - 2 * 80000 - 8192
        budget.spent_output = 2000
        budget.final_input_estimate = 80000
        self.assertTrue(budget.request_can_research(20000))
        self.assertFalse(budget.request_can_research(20001))
        output = budget.request_output_limit(20000, allow_tools=True)
        self.assertEqual(output, ASSESSMENT_LIMITS.output_tokens - 2000 - 49152 - 8192)
        budget.spent_output += output
        self.assertEqual(budget.request_output_limit(60000, allow_tools=False, final=True), 49152)

    async def test_unaffordable_prepared_review_selects_final_protocol_before_provider_call(self):
        content = json.dumps({'intent': 'Choose the supported next step',
            'intent_source': 'request', 'discussion_message_ids': [],
            'settled_choices': [], 'open_choices': [], 'facts': 'Complete selected account table.',
            'question': '', 'recommendation': ''})
        flow, session, _, _ = cost.RequestCostTests().flow([
            AgentStep(content, (), AgentUsage(160000, 1200))])
        flow.assessment_requested = True
        flow.budget.request_limits = ASSESSMENT_LIMITS
        flow.plan = {'finish_after_reads': False}
        flow.decision = SimpleNamespace(rounds=(SimpleNamespace(tool_calls=()),))
        flow.new_evidence = True
        flow.result_views['read'] = {'value': 7}

        async def prepare(**kwargs):
            # A backend can prepare more input than the original transport
            # estimate. It still needs the correct final protocol if refused.
            flow.budget.next_input_estimate = 160000
            return True

        flow._prepare_answer_session = AsyncMock(side_effect=prepare)
        answer, step = await flow._answer_round({'read': {'value': 7}})
        self.assertEqual(answer, content)
        self.assertIsNone(step)
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][1])
        self.assertEqual(session.calls[0][2], AgentReasoningEffort.MAX)
        self.assertEqual(session.output_limits, [49152])
        self.assertIn('intent_source', session.instructions[0])
        self.assertTrue(flow.rounder.final_attempted)
        self.assertEqual([call.kwargs.get('allow_more', False)
                          for call in flow._prepare_answer_session.await_args_list], [True, False])
