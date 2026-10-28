"""GAPS-FIX-2 regression: CompleteReadCompositionTests."""
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch
from elbow_helper.agent.conversation.context import estimate_tokens
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.families.core import family_tool
from elbow_helper.agent.plan.format import answer_instructions
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from agent.engine.test_agent_plan_flow import _context, _Model, _Session, _plan, _model_step
from agent.engine.test_planner_fix2_evidence import unpack


class CaptureModel(_Model):
    def create_agent_session(self, **kwargs):
        if self.request is not None:
            self.writer = kwargs
            return _Session([], [])
        return super().create_agent_session(**kwargs)


def decision_payload(facts='Supported conclusion.'):
    return json.dumps({'intent': 'Established objective', 'intent_source': 'request',
        'discussion_message_ids': [], 'settled_choices': [], 'open_choices': [], 'facts': facts, 'question': '', 'recommendation': ''})


class CompleteReadCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_instruction_boundary_still_calls_provider_and_retains_citation_sources(self):
        from elbow_helper.agent.engine.assessment import discussion_ids
        from elbow_helper.agent.engine.budgets import ASSESSMENT_LIMITS
        from elbow_helper.agent.families.core import FamilyReport
        from elbow_helper.agent.prompts import LIMIT_ANSWER_INSTRUCTION
        context = _context()
        tool = family_tool('read_conversation_evidence', 'Read discussion.', {}, AsyncMock())
        with patch('elbow_helper.agent.engine.service.build_agent_tools',
                   return_value={'read_conversation_evidence': tool}):
            flow, _ = AgentService(_Model(_Session([], [])))._start_flow(
                question='Assess a synthetic decision', local_context='', context=context, conversation_history='')
        sessions = []
        def factory(**options):
            session = _Session([AgentStep(decision_payload(), (), AgentUsage(1000, 20))], [])
            sessions.append(session)
            return session
        flow.final_session_factory = factory
        rows = [{'message_id': 100000 + i, 'content': 'A synthetic decision.'} for i in range(1000)]
        report = FamilyReport('discussion', context.guild.id, 'read_conversation_evidence', json.dumps({
            'rows': rows, 'summary': {'requester_id': context.member.id, 'mode': 'channel_window', 'channel_id': 91}}))
        flow.runner.report_snapshots['discussion'] = (report,
            frozenset({context.source_message.channel.id}), frozenset())
        flow.completed_results['discussion'] = report.page(limit=1)
        flow.result_views['discussion'] = report.page(limit=1)
        flow.completed_results['members'] = {'family': 'read_member_accounts', 'rows': []}
        flow.assessment_requested = True
        flow.budget.request_limits = ASSESSMENT_LIMITS
        self.assertTrue(await flow._prepare_answer_session(instruction=LIMIT_ANSWER_INSTRUCTION))
        # The full evidence fits before framing; the final continuation puts it
        # one token over. Evidence must shrink before preparing the actual call.
        remaining = flow.budget.next_input_estimate + estimate_tokens(LIMIT_ANSWER_INSTRUCTION) - 1
        flow.budget.spent_input = ASSESSMENT_LIMITS.input_tokens - remaining
        self.assertEqual(await flow._compact_reply(), decision_payload())
        self.assertEqual(len(sessions[-1].calls), 1)
        self.assertEqual(sessions[-1].calls[0][2], AgentReasoningEffort.MAX)
        self.assertIn(100999, discussion_ids(context.state.grounding_evidence))

    async def test_plain_assessment_draft_gets_a_small_format_repair_within_the_request_budget(self):
        source = AsyncMock(return_value={'rows': [{'value': 7}]})
        tool = family_tool('read_community_reference', 'Read a value.', {}, source)
        plan = {**_plan([{'id': 'facts', 'capability': 'read_community_reference',
            'arguments': {}, 'depends_on': []}]), 'assessment': True, 'finish_after_reads': True}
        sessions = [_Session([_model_step(plan)], []),
            _Session([AgentStep('Seven volunteers are confirmed.', (), AgentUsage(1000, 20))], []),
            _Session([AgentStep(decision_payload('Seven volunteers are confirmed.'), (), AgentUsage(1000, 20))], [])]
        requests = []

        class Model:
            def create_agent_session(self, **kwargs):
                requests.append(kwargs)
                return sessions[len(requests) - 1]

        with patch('elbow_helper.agent.engine.service.build_agent_tools',
                   return_value={'read_community_reference': tool}):
            flow, _ = AgentService(Model())._start_flow(question='Assess the supplied values',
                local_context='', context=_context(), conversation_history='')
        self.assertEqual(await flow.run(), 'Seven volunteers are confirmed.')
        self.assertEqual(sessions[1].calls[0][2], AgentReasoningEffort.MAX)
        self.assertEqual(sessions[2].calls[0][2], AgentReasoningEffort.NONE)
        self.assertIn('It replaces the normal plain reply', requests[1]['system_prompt'])
        self.assertEqual(json.loads(requests[2]['prompt'])['draft'], 'Seven volunteers are confirmed.')
        self.assertNotIn('Available evidence', requests[2]['prompt'])

    async def test_tool_enabled_assessment_prose_still_gets_max_final_composition(self):
        source = AsyncMock(return_value={'error': 'Unavailable source', 'cause': 'source_unavailable'})
        tool = family_tool('read_community_reference', 'Read a value.', {}, source)
        registry = {'read_community_reference': tool}
        plan = {**_plan([{'id': 'facts', 'capability': 'read_community_reference',
            'arguments': {}, 'depends_on': []}]), 'assessment': True}
        sessions = [_Session([replace(_model_step(plan), usage=AgentUsage(1000, 100))], []),
            _Session([AgentStep('Tentative prose.', (), AgentUsage(1000, 20))], []),
            _Session([AgentStep(decision_payload(), (), AgentUsage(1000, 20))], [])]
        requests = []

        class Model:
            def create_agent_session(self, **kwargs):
                requests.append(kwargs)
                return sessions[len(requests) - 1]

        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
            flow, _ = AgentService(Model())._start_flow(question='Assess the supplied values',
                local_context='', context=_context(), conversation_history='')
        self.assertEqual(await flow.run(), 'Supported conclusion.')
        self.assertEqual(sessions[1].calls[0][2], AgentReasoningEffort.HIGH)
        self.assertEqual(sessions[2].calls[0][2], AgentReasoningEffort.MAX)

    async def test_assessment_final_reserves_max_reasoning_and_repairs_a_cutoff(self):
        for cutoff in (False, True):
            with self.subTest(cutoff=cutoff):
                source = AsyncMock(return_value={'rows': [{'value': 7}]})
                tool = family_tool('read_community_reference', 'Read a value.', {}, source)
                registry = {'read_community_reference': tool}
                plan = {**_plan([{'id': 'facts', 'capability': 'read_community_reference',
                    'arguments': {}, 'depends_on': []}]), 'assessment': True, 'finish_after_reads': True}
                first = AgentStep('Incomplete fragment' if cutoff else decision_payload(), (),
                    AgentUsage(1000, 49152 if cutoff else 20), output_limit_reached=cutoff)
                planning = replace(_model_step(plan), usage=AgentUsage(1000, 100))
                sessions = [_Session([planning], []), _Session([first], [])]
                if cutoff:
                    sessions.append(_Session([AgentStep(decision_payload(), (), AgentUsage(1000, 30))], []))
                requests = []

                class Model:
                    def create_agent_session(self, **kwargs):
                        requests.append(kwargs)
                        return sessions[len(requests) - 1]

                with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
                    flow, _ = AgentService(Model())._start_flow(question='Assess the supplied values',
                        local_context='', context=_context(), conversation_history='')
                self.assertEqual(await flow.run(), 'Supported conclusion.')
                self.assertEqual(len(requests), 3 if cutoff else 2)
                self.assertIn('Available evidence', requests[1]['prompt'])
                self.assertEqual(sessions[1].calls[0][2], AgentReasoningEffort.MAX)
                self.assertEqual(sessions[1].output_limits[0], 49152)
                self.assertEqual(len(sessions[1].calls), 1)
                self.assertIs(flow.rounder.session, sessions[-1])
                if cutoff:
                    self.assertEqual(sessions[2].calls[0][2], AgentReasoningEffort.NONE)
                    self.assertEqual(requests[1]['prompt'], requests[2]['prompt'])
                self.assertLess(flow.budget.spent_output, flow.budget.request_limits.output_tokens)

    async def test_decision_repairs_do_not_reintroduce_internal_json_after_composition(self):
        from elbow_helper.agent.engine.assessment import DECISION_ANSWER_INSTRUCTION
        model = CaptureModel(_Session([AgentStep('Corrected public prose.', (), AgentUsage(1000, 10))], []))
        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value={}):
            flow, _ = AgentService(model)._start_flow(question='Assess the supplied values',
                local_context='', context=_context(), conversation_history='')
        flow.assessment_requested = True
        flow.decision_composed = True
        flow.rounder.final_attempted = True
        reply = await flow._advance_answer(allow_tools=False, continuation_instruction='Correct the cited source.')
        self.assertEqual(reply.content, 'Corrected public prose.')
        self.assertNotIn(DECISION_ANSWER_INSTRUCTION, model.session.instructions[-1])

    async def test_complete_simple_plan_composes_without_resending_replanning_catalogue(self):
        for result, can_replan in (({'rows': [{'value': 7}]}, False),
                ({'error': 'Missing source', 'cause': 'source_unavailable'}, True),
                ({'rows': [], 'selection_complete': False}, True),
                ({'rows': [], 'missing_sources': [{'cause': 'source_unavailable'}]}, True),
                ({'rows': [], 'mode': 'index'}, True),
                ({'rows': [], 'source_next_cursor': 'older-history'}, True)):
            with self.subTest(result=result):
                source = AsyncMock(return_value=result)
                tool = family_tool('read_community_reference', 'Read facts.', {}, source)
                registry = {'read_community_reference': tool, 'hidden': replace(tool, default_visible=False)}
                plan = {**_plan([{'id': 'facts', 'capability': 'read_community_reference',
                    'arguments': {}, 'depends_on': []}]), 'finish_after_reads': True}
                initial = _Session([_model_step(plan)], [])
                final = _Session([AgentStep('Synthetic answer.', (), AgentUsage(1000, 20))], [])

                class Model(CaptureModel):
                    def create_agent_session(self, **kwargs):
                        if self.request is not None:
                            self.writer = kwargs
                            return final
                        return super().create_agent_session(**kwargs)

                model = Model(initial)
                with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
                    flow, _ = AgentService(model)._start_flow(question='Give the requested fact',
                        local_context='', context=_context(), conversation_history='')
                self.assertEqual(await flow.run(), 'Synthetic answer.')
                self.assertEqual(len(initial.calls), 1)
                self.assertEqual(len(final.calls), 1)
                self.assertEqual(bool(model.writer['tools']), can_replan)
                source.assert_awaited_once()

    async def test_standalone_war_snapshot_restores_every_row_after_transport_paging(self):
        context = _context()
        rows = [{'member_id': 1000 + i, 'player_tag': '#P' + str(i), 'player_name': f'Player {i}',
            'attacks_used': i, 'missed': i % 3, 'totals_complete': True,
            'war_ids': ['war-' + str(j) + '-' + 'x' * 80 for j in range(10)]} for i in range(54)]
        source = AsyncMock(return_value={'rows': rows, 'war_ids': ['war-' + str(j) for j in range(10)],
            'coverage': {'historical_collection_complete': False}})
        tool = family_tool('read_war_attendance', 'Read war attendance.', {}, source)
        model = CaptureModel(_Session([], []))
        registry = {'read_war_attendance': tool, 'hidden': replace(tool, default_visible=False)}
        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
            flow, _ = AgentService(model)._start_flow(question='Compare the whole war cohort',
                local_context='', context=context, conversation_history='')
        results = await flow.runner.run(_plan([{'id': 'wars', 'capability': 'read_war_attendance',
            'arguments': {'limit': 16}, 'depends_on': []}]))
        self.assertEqual(len(results['wars']['rows']), 16)
        flow.completed_results.update(results)
        flow.result_views.update(flow.runner.model_results_for(results))
        self.assertTrue(await flow._prepare_answer_session(allow_more=True))
        evidence = unpack(json.loads(model.writer['prompt'].split('\nAvailable evidence:\n', 1)[1]))
        visible = evidence['results']['wars']
        self.assertEqual(len(visible['rows']), 54)
        self.assertEqual([row['attacks_used'] for row in visible['rows']], list(range(54)))
        self.assertTrue(all(row['wars_observed'] == 10 for row in visible['rows']))
        self.assertNotIn('evidence_complete', visible)
        self.assertFalse(visible['coverage']['historical_collection_complete'])
        self.assertNotIn('war_ids', visible['rows'][0])
        retained = json.loads(context.state.reports[results['wars']['report_id']].payload)
        self.assertEqual(retained['rows'], rows)
        source.assert_awaited_once()

    async def test_standalone_ranked_final_keeps_the_complete_table_beyond_transport_room(self):
        rows = [{'player_tag': f'#P{i}', 'player_name': f'Account {i}',
                 'attacks_used': i, 'attacks_available': i + 1,
                 'counts_complete': False} for i in range(200)]
        source = AsyncMock(return_value={'rows': rows})
        tool = family_tool('read_ranked_activity', 'Read ranked activity.', {}, source)
        model = CaptureModel(_Session([], []))
        with patch('elbow_helper.agent.engine.service.build_agent_tools',
                   return_value={'read_ranked_activity': tool}):
            flow, _ = AgentService(model)._start_flow(question='Compare the selected ranked accounts',
                local_context='', context=_context(), conversation_history='')
        results = await flow.runner.run(_plan([{'id': 'ranked', 'capability': 'read_ranked_activity',
            'arguments': {'limit': 1}, 'depends_on': []}]))
        self.assertEqual(len(results['ranked']['rows']), 1)
        flow.completed_results.update(results)
        flow.result_views.update(flow.runner.model_results_for(results))
        with patch('elbow_helper.agent.engine.budgets.MAX_EVIDENCE_CHARACTERS', 3000):
            self.assertTrue(await flow._prepare_answer_session())
        evidence = unpack(json.loads(model.writer['prompt'].split('\nAvailable evidence:\n', 1)[1]))
        visible = evidence['results']['ranked']['rows']
        self.assertEqual([{key: row[key] for key in rows[0]} for row in visible], rows)
        source.assert_awaited_once()

    def test_followup_prompt_keeps_action_semantics_with_bounded_prefix(self):
        registry = {name: tool for name, tool in build_agent_tools().items() if tool.default_visible}
        prompt = answer_instructions(registry)
        self.assertIn('depends_on', prompt)
        # Include the shared assessment rules while staying below the former
        # 61k-character follow-up prefix, before evidence.
        self.assertLess(estimate_tokens(prompt), 12_000)
