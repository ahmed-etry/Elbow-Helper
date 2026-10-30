"""Decision disposition and discussion provenance before any recommendation."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.assessment import (
    assessment_limits, decision_answer, discussion_ids, read_decision_discussions, select_decision_sources,
)
from elbow_helper.infrastructure.ai import AgentToolCall
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.agent.plan.checker import check_plan
from elbow_helper.agent.families.core import family_tool


def payload(**values):
    return json.dumps({'intent': 'A missing decision', 'intent_source': 'unresolved',
        'discussion_message_ids': [], 'settled_choices': [], 'open_choices': [], 'facts': 'Verified value: 7.',
        'question': 'Which objective should guide the decision?',
        'recommendation': 'This advice must not be delivered.', **values})


class DecisionContextTests(unittest.IsolatedAsyncioTestCase):
    def test_separate_owner_profiles_preserve_unknown_leadership_selections(self):
        rows = [{'member_id': owner, 'player_tag': tag, 'player_name': tag, 'clan_code': 'BEH'}
                for owner, tag in ((42, '#P0'), (43, '#Q0'), (43, '#Y0'), (None, '#L0'))]
        members = [{'member_id': 42, 'leadership': {'status': 'none'}},
                   {'member_id': 43, 'leadership': {'status': 'unknown'}}]
        for profiles in (members, {'columns': ['member_id', 'leadership'],
                                  'rows': [[row['member_id'], row['leadership']] for row in members]}):
            result = assessment_limits({'roster': {'rows': rows, 'members': profiles,
                'leadership_filter': {'unverified_accounts': 3}}})
            self.assertEqual({row['player_tag'] for row in result['unverified_selection']},
                             {'#Q0', '#Y0', '#L0'})
        self.assertTrue(all('leadership' not in row for row in rows))

    def test_unresolved_selection_identities_are_deduplicated_across_rosters_and_cohorts(self):
        account = {'player_tag': '#P0', 'player_name': 'First', 'clan_code': 'alpha'}
        unlinked = {**account, 'member_id': None, 'leadership': {'status': 'unknown'}}
        departed = {'player_tag': '#P1', 'player_name': 'Second', 'clan_code': 'beta',
                    'member_id': 91, 'leadership': {'status': 'unknown'}}
        result = assessment_limits({
            'roster': {'leadership_filter': {'unverified_accounts': 2}, 'rows': [unlinked, departed]},
            'cohort': {'leadership_filter': {'unverified_accounts': 1}, 'rows': [
                {'member_id': None, 'leadership': {'status': 'unknown'}, 'accounts': [account]}]}})
        self.assertEqual(result['unverified_selection'], [account,
            {key: departed[key] for key in ('player_tag', 'player_name', 'clan_code')}])
        self.assertEqual(unlinked['leadership'], {'status': 'unknown'})

    def test_unfiltered_failed_or_known_owner_rows_do_not_create_selection_caveats(self):
        unknown = {'player_tag': '#P0', 'leadership': {'status': 'unknown'}}
        known = {'player_tag': '#P1', 'leadership': {'status': 'none'}}
        self.assertEqual(assessment_limits({
            'unfiltered': {'rows': [unknown]},
            'failed': {'error': 'Unavailable', 'leadership_filter': {'unverified_accounts': 1},
                       'rows': [unknown]},
            'known': {'leadership_filter': {'unverified_accounts': 1}, 'rows': [known]}}), {})

    def test_compact_selection_rows_keep_the_same_unresolved_identity(self):
        result = assessment_limits({'roster': {'leadership_filter': {'unverified_accounts': 1},
            'rows': {'columns': ['player_tag', 'player_name', 'clan_code', 'leadership'],
                     'rows': [['#P0', 'First', 'alpha', {'status': 'unknown'}]]}}})
        self.assertEqual(result['unverified_selection'], [
            {'player_tag': '#P0', 'player_name': 'First', 'clan_code': 'alpha'}])

    async def test_source_discovery_uses_only_request_and_authorized_metadata_and_restores_the_plan(self):
        for selected, expected, call_name in (([92], [92], 'select_discussion_sources'),
                ([93], [], 'select_discussion_sources'), ([92], [], 'unrelated_tool'),
                ([92], [], None)):
            with self.subTest(selected=selected, call_name=call_name):
                requests = []
                original, navigation = object(), object()

                def factory(**options):
                    requests.append(options)
                    return navigation

                rounder = SimpleNamespace(session=original, request_effort=AgentReasoningEffort.HIGH,
                    pending_calls=('pending plan',), pending_results=(), unpublished=None,
                    partial_content='Existing draft framing', last_allow_tools=True,
                    advance=AsyncMock(return_value=SimpleNamespace(
                        content=json.dumps({'channel_ids': selected}) if call_name is None else '',
                        tool_calls=((AgentToolCall('select', call_name,
                            json.dumps({'channel_ids': selected})),) if call_name else ()))))
                flow = SimpleNamespace(final_session_factory=factory, rounder=rounder,
                    budget=SimpleNamespace(next_input_estimate=300,
                        request_output_limit=lambda *args, **kwargs: 2048),
                    context=SimpleNamespace(state=SimpleNamespace(request_text='Plan the next session'),
                        guild=SimpleNamespace(get_channel_or_thread=lambda identifier: None)))
                with patch('elbow_helper.agent.engine.assessment.scoped_channel_directory',
                           AsyncMock(return_value=json.dumps({'91': 'First room', '92': 'Second room'}))), \
                     patch('elbow_helper.agent.engine.assessment.require_evidence_access', AsyncMock()):
                    self.assertEqual(await select_decision_sources(flow), expected)
                self.assertIs(rounder.session, original)
                self.assertEqual(rounder.pending_calls, ('pending plan',))
                self.assertEqual(rounder.request_effort, AgentReasoningEffort.HIGH)
                self.assertEqual(flow.budget.next_input_estimate, 300)
                self.assertNotIn('Existing draft framing', requests[0]['prompt'])
                self.assertEqual(rounder.advance.await_args.kwargs['reasoning_effort'], AgentReasoningEffort.NONE)
                self.assertTrue(rounder.advance.await_args.kwargs['allow_tools'])
                self.assertEqual(requests[0]['tools'][0].name, 'select_discussion_sources')

    def test_unresolved_intent_ends_with_one_question_after_any_requested_advice(self):
        self.assertEqual(decision_answer(payload(recommendation=''), set()),
            'Verified value: 7.\n\nWhich objective should guide the decision?')
        # A requested recommendation is given under its stated assumption, then the question.
        self.assertEqual(decision_answer(payload(recommendation='Assuming growth: move the active group.'), set()),
            'Verified value: 7.\n\nAssuming growth: move the active group.\n\n'
            'Which objective should guide the decision?')

    def test_established_discussion_intent_requires_a_read_message(self):
        with self.assertRaises(ValueError):
            decision_answer(payload(intent_source='discussion', question=''), set())
        with self.assertRaises(ValueError):
            decision_answer(payload(intent_source='discussion', question='', discussion_message_ids=[91]), {92})
        self.assertIn('advice', decision_answer(payload(intent_source='discussion', question='',
            discussion_message_ids=[91]), {91}))

    def test_unresolved_intent_requires_a_question(self):
        with self.assertRaises(ValueError):
            decision_answer(payload(question=''), set())

    def test_an_explicit_goal_does_not_resolve_an_open_operational_choice(self):
        content = payload(intent='Open event signups', intent_source='request',
            open_choices=['One session or two sessions'],
            facts='Seven volunteers are confirmed.', recommendation='',
            question='Do you want one session or two?')
        self.assertEqual(decision_answer(content, set()),
            'Seven volunteers are confirmed.\n\nDo you want one session or two?')
        with self.assertRaises(ValueError):
            decision_answer(payload(intent_source='request', open_choices=['Session format'], question=''), set())

    def test_only_actual_discussion_rows_can_establish_intent(self):
        results = {'nearby': {'rows': [{'message_id': 1}]},
            'denied': {'family': 'read_conversation_evidence', 'error': 'Denied', 'rows': [{'message_id': 2}]},
            'read': {'family': 'read_conversation_evidence', 'rows': {
                'columns': ['message_id', 'content'], 'rows': [[3, 'A decision.']]}}}
        self.assertEqual(discussion_ids(results), {3})

    def test_planning_requires_discussion_source_selection(self):
        tool = family_tool('read_conversation_evidence', 'Read a discussion.', {}, AsyncMock())
        plan = {'goal': 'Assess a decision', 'effort': 'max', 'output': 'text', 'assessment': True,
            'steps': [{'id': 'discussion', 'capability': 'read_conversation_evidence',
                       'arguments': {}, 'depends_on': []}]}
        self.assertFalse(check_plan(plan, {'read_conversation_evidence': tool}).ok)
        plan['discussion_channels'] = [91]
        self.assertTrue(check_plan(plan, {'read_conversation_evidence': tool}).ok)

    async def test_selected_discussion_is_read_once_through_the_normal_runner(self):
        result = {'decision_discussion_91': {'family': 'read_conversation_evidence', 'rows': []}}
        flow = SimpleNamespace(assessment_requested=True, registry={'read_conversation_evidence': object()},
            plan={'discussion_channels': [91]}, steps_by_id={}, completed_results={}, read_results={},
            result_views={}, model_results=lambda values: values,
            runner=SimpleNamespace(run=AsyncMock(return_value=result), model_results_for=lambda values: values))
        await read_decision_discussions(flow)
        step = flow.runner.run.await_args.args[0]['steps'][0]
        self.assertEqual(step['arguments'], {'mode': 'channel_window', 'channel_id': 91, 'limit': 100})
        self.assertIn('decision_discussion_91', flow.read_results)
        await read_decision_discussions(flow)
        flow.runner.run.assert_awaited_once()

    async def test_known_assessment_subjects_use_one_search_across_selected_channels(self):
        for selectors, evidence, previous in (
            ({'member_ids': [42, 43]}, {}, {}),
            ({'member_ids': {'step': 'members', 'path': ['rows', '*', 'member_id']}},
             {'members': {'family': 'read_member_cohort', 'rows': [
                 {'member_id': 42}, {'member_id': 43}, {'member_id': 42}]}}, {}),
            ({}, {'members': {'family': 'read_member_accounts', 'members': [
                {'member_id': 42}, {'member_id': 43}], 'rows': []}}, {}),
            ({}, {}, {42: 'Synthetic first', 43: 'Synthetic second'}),
        ):
            with self.subTest(selectors=selectors, evidence=evidence):
                result = {f'decision_discussion_search_{index}':
                          {'family': 'read_conversation_evidence', 'rows': []} for index in (0, 1)}
                flow = SimpleNamespace(assessment_requested=True, registry={'read_conversation_evidence': object()},
                    context=SimpleNamespace(state=SimpleNamespace(resolved_members=previous)),
                    plan={'discussion_channels': [91, 92], 'steps': [{'id': 'measure',
                          'capability': 'read_war_attendance', 'arguments': selectors}]},
                    steps_by_id={}, completed_results=dict(evidence), read_results={}, result_views={},
                    model_results=lambda values: values,
                    runner=SimpleNamespace(run=AsyncMock(return_value=result), model_results_for=lambda values: values))
                await read_decision_discussions(flow)
                steps = flow.runner.run.await_args.args[0]['steps']
                self.assertEqual([step['arguments'] for step in steps], [
                    {'mode': 'search', 'author_ids': [42, 43], 'channel_ids': [91, 92]},
                    {'mode': 'search', 'mentions': [42, 43], 'channel_ids': [91, 92]}])
                self.assertEqual(len({step['id'] for step in steps}), 2)
                await read_decision_discussions(flow)
                flow.runner.run.assert_awaited_once()

    async def test_known_subject_search_replaces_planned_windows_and_preserves_dependents(self):
        result = {f'decision_discussion_search_{index}':
                  {'family': 'read_conversation_evidence', 'rows': []} for index in (0, 1)}
        flow = SimpleNamespace(assessment_requested=True, registry={'read_conversation_evidence': object()},
            plan={'discussion_channels': [91, 92], 'steps': [
                {'id': 'first', 'capability': 'read_conversation_evidence',
                 'arguments': {'mode': 'channel_window', 'channel_id': 91}},
                {'id': 'second', 'capability': 'read_conversation_evidence',
                 'arguments': {'mode': 'channel_window', 'channel_id': 92}},
                {'id': 'measure', 'capability': 'read_war_attendance', 'depends_on': ['first', 'second'],
                 'arguments': {'member_ids': [42], 'extra': {'step': 'second', 'path': ['rows']}}}]},
            steps_by_id={}, completed_results={}, read_results={}, result_views={},
            model_results=lambda values: values,
            runner=SimpleNamespace(run=AsyncMock(return_value=result), model_results_for=lambda values: values))
        await read_decision_discussions(flow)
        self.assertEqual(len(flow.plan['steps']), 1)
        remaining = flow.plan['steps'][0]
        self.assertEqual(remaining['depends_on'], ['decision_discussion_search_0', 'decision_discussion_search_1'])
        self.assertEqual(remaining['arguments']['extra']['step'], 'decision_discussion_search_0')
        self.assertEqual([step['arguments'] for step in flow.runner.run.await_args.args[0]['steps']], [
            {'mode': 'search', 'author_ids': [42], 'channel_ids': [91, 92]},
            {'mode': 'search', 'mentions': [42], 'channel_ids': [91, 92]}])

    async def test_planned_member_lookup_runs_before_deciding_how_to_read_discussions(self):
        for found, expected in (({'family': 'read_member_accounts', 'rows': [{'member_id': 42}]}, 'search'),
                                ({'error': 'Lookup unavailable'}, 'channel_window')):
            with self.subTest(found=found):
                member_step = {'id': 'members', 'capability': 'read_member_accounts',
                               'arguments': {'name': 'Synthetic Member'}, 'depends_on': []}
                calls = []
                async def run(plan):
                    calls.append(plan)
                    step = plan['steps'][0]
                    if step['id'] == 'members':
                        return {'members': found}
                    return {step['id']: {'family': 'read_conversation_evidence', 'rows': []}}
                flow = SimpleNamespace(assessment_requested=True,
                    registry={'read_conversation_evidence': object(), 'read_member_accounts':
                              family_tool('read_member_accounts', 'Look up members.', {}, AsyncMock())},
                    plan={'discussion_channels': [91], 'steps': [member_step]},
                    steps_by_id={}, completed_results={}, read_results={}, result_views={},
                    model_results=lambda values: values,
                    runner=SimpleNamespace(run=AsyncMock(side_effect=run), model_results_for=lambda values: values))
                results = await read_decision_discussions(flow)
                self.assertEqual(calls[0]['steps'], [member_step])
                self.assertEqual(calls[1]['steps'][0]['arguments']['mode'], expected)
                if expected == 'search':
                    self.assertEqual(calls[1]['steps'][0]['arguments']['author_ids'], [42])
                    self.assertEqual(calls[1]['steps'][1]['arguments']['mentions'], [42])
                    self.assertNotIn('mentions', calls[1]['steps'][0]['arguments'])
                self.assertIn('members', results)
                self.assertEqual(flow.plan['steps'], [])

    async def test_equivalent_planned_subject_search_replaces_windows_without_duplicate_search(self):
        search = {'id': 'search', 'capability': 'read_conversation_evidence', 'depends_on': [],
                  'arguments': {'mode': 'search', 'author_ids': [42], 'channel_ids': [91]}}
        mention = {'id': 'mention', 'capability': 'read_conversation_evidence', 'depends_on': [],
                   'arguments': {'mode': 'search', 'mentions': [42], 'channel_ids': [91]}}
        window = {'id': 'window', 'capability': 'read_conversation_evidence',
                  'arguments': {'mode': 'channel_window', 'channel_id': 91}}
        flow = SimpleNamespace(assessment_requested=True, registry={'read_conversation_evidence': object()},
            context=SimpleNamespace(state=SimpleNamespace(resolved_members={42: 'Synthetic'})),
            plan={'discussion_channels': [91], 'steps': [search, mention, window]},
            steps_by_id={}, completed_results={}, runner=SimpleNamespace(run=AsyncMock()))
        self.assertEqual(await read_decision_discussions(flow), {})
        self.assertEqual(flow.plan['steps'], [search, mention])
        flow.runner.run.assert_not_awaited()

    async def test_discussion_already_in_the_plan_is_not_fetched_twice(self):
        flow = SimpleNamespace(assessment_requested=True, registry={'read_conversation_evidence': object()},
            plan={'discussion_channels': [91], 'steps': [{'id': 'discussion',
                'capability': 'read_conversation_evidence',
                'arguments': {'mode': 'channel_window', 'channel_id': 91}}]},
            steps_by_id={}, completed_results={}, runner=SimpleNamespace(run=AsyncMock()))
        with patch('elbow_helper.agent.engine.assessment.select_decision_sources',
                   AsyncMock()) as discover:
            self.assertEqual(await read_decision_discussions(flow), {})
        discover.assert_not_awaited()
        flow.runner.run.assert_not_awaited()

    async def test_discovered_rooms_join_the_scoped_read_plan_once(self):
        results = {f'decision_discussion_{identifier}': {
            'family': 'read_conversation_evidence', 'rows': []} for identifier in (91, 92)}
        flow = SimpleNamespace(assessment_requested=True, registry={'read_conversation_evidence': object()},
            plan={'discussion_channels': [91]}, steps_by_id={}, completed_results={}, read_results={},
            result_views={}, model_results=lambda values: values,
            runner=SimpleNamespace(run=AsyncMock(return_value=results), model_results_for=lambda values: values))
        with patch('elbow_helper.agent.engine.assessment.select_decision_sources',
                   AsyncMock(return_value=[92])) as discover:
            await read_decision_discussions(flow)
            selected = [step['arguments']['channel_id'] for step in flow.runner.run.await_args.args[0]['steps']]
            self.assertEqual(selected, [92, 91])
            await read_decision_discussions(flow)
            discover.assert_awaited_once()
        flow.runner.run.assert_awaited_once()
