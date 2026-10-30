"""Ambiguous public reads cover candidates without authorizing a subject."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.actions.contracts import ActionClass
from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.engine.tool_call import (
    clone_tool_state, execute_tool, merge_tool_state, tool_state_snapshot,
)
from elbow_helper.agent.families import account_names, cohort_activity, identity_reads
from elbow_helper.agent.families.catalogue import FAMILY_INSTRUCTION
from elbow_helper.agent.families.model_view import family_model_view
from elbow_helper.agent.plan.executor import execute_plan
from agent.families.test_named_account_stats import lookup_context, named_member, rosters


class AmbiguousCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_candidates_feed_activity_reference_without_target_authorization(self):
        activity = Mock(return_value={identifier: {
            'status': 'observed', 'last_active_day': day,
            'active_streak_days': 1, 'lifetime_messages': identifier}
            for identifier, day in ((42, '2026-01-11'), (43, '2026-01-12'))})
        context = replace(lookup_context([named_member(42, 'Silver Kite'),
                                          named_member(43, 'Silver Kite')]),
                          achievement_queries=SimpleNamespace(member_activity=activity))
        tools = {tool.definition.name: tool for tool in identity_reads.TOOLS}
        plan = {'steps': [
            {'id': 'resolve', 'capability': 'read_member_accounts', 'depends_on': [],
             'arguments': {'name': 'Silver Kite', 'look_in': 'discord'}},
            {'id': 'activity', 'capability': 'read_member_cohort', 'depends_on': ['resolve'],
             'arguments': {'basis': 'selected', 'include_activity': True,
                           'after': '2026-01-01T00:00:00+00:00',
                           'before': '2026-02-01T00:00:00+00:00',
                           'member_ids': {'step': 'resolve', 'path': ['members', '*', 'member_id']}}}]}

        async def run(step, arguments, earlier):
            local = replace(context, state=clone_tool_state(context.state))
            previous = tool_state_snapshot(local)
            result = await tools[step['capability']].handler(local, arguments)
            merge_tool_state(context, local, previous)
            return result

        with patch.object(identity_reads, 'profiles', AsyncMock(return_value={})), \
             patch.object(cohort_activity, 'selected_wars', AsyncMock(return_value={'rows': []})):
            results = await execute_plan(plan, run)
        lookup = results['resolve']
        self.assertEqual(lookup['status'], 'resolved')
        self.assertEqual(lookup['name_resolutions'][0]['status'], 'ambiguous')
        self.assertEqual(lookup['resolved_member_ids'], [])
        self.assertTrue(lookup['selection_complete'])
        for view in (lookup, family_model_view(lookup), answer_evidence({'lookup': lookup})['lookup']):
            self.assertEqual([row['member_id'] for row in view['members']], [42, 43])
            self.assertTrue(all(row['candidate_for'] == ['Silver Kite'] for row in view['members']))
        self.assertNotIn('error', results['activity'])
        self.assertEqual([row['last_active_day'] for row in results['activity']['rows']],
                         ['2026-01-11', '2026-01-12'])
        activity.assert_called_once_with([42, 43])
        self.assertEqual(context.state.resolved_members, {})
        for identifier in (42, 43):
            action = AsyncMock()
            result = json.loads(await execute_tool(name='synthetic_action', handler=action,
                context=context, arguments={'member_id': identifier}, action_class=ActionClass.OUTPUT))
            self.assertIn('error', result)
            action.assert_not_awaited()
        # A genuinely unique lookup can still settle the identity later.
        await identity_reads.member_accounts(context, {'name': 'synthetic_42', 'look_in': 'discord'})
        self.assertEqual(context.state.resolved_members, {42: 'Silver Kite'})

    async def test_game_candidates_keep_query_provenance_and_unknown_miss_needs_input(self):
        context = lookup_context([named_member(42, 'Amber Pilot'), named_member(43, 'Copper Pilot')],
                                 [('#P0', 'Silver Kite', 42), ('#Q0', 'Silver Kite', 43)])
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())), \
             patch.object(identity_reads, 'profiles', AsyncMock(return_value={})):
            for queries, expected in ((['Silver Kite'], 'resolved'),
                                      (['Silver Kite', 'Absent Subject'], 'needs_input')):
                result = await identity_reads.member_accounts(context, {
                    'account_names': queries, 'look_in': 'game'})
                self.assertEqual(result['status'], expected)
                self.assertEqual([row['player_tag'] for row in result['rows']], ['#P0', '#Q0'])
                self.assertTrue(all(row['candidate_for'] == ['Silver Kite'] for row in result['rows']))
                self.assertEqual(result['resolved_member_ids'], [])
                self.assertEqual(context.state.resolved_members, {})
            empty = await identity_reads.member_accounts(context, {'name': 'Absent Subject', 'look_in': 'game'})
        self.assertEqual(empty['status'], 'needs_input')
        self.assertEqual(empty['rows'], [])

    async def test_overlapping_candidate_queries_keep_both_names(self):
        context = lookup_context([named_member(42, 'Amber Pilot'), named_member(43, 'Copper Pilot')],
                                 [('#P0', 'Silver Kite', 42), ('#Q0', 'Silver Kite', 43)])
        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())), \
             patch.object(identity_reads, 'profiles', AsyncMock(return_value={})):
            result = await identity_reads.member_accounts(context, {
                'account_names': ['Silver Kite', 'Silver Kte'], 'look_in': 'game'})
        self.assertEqual(len(result['rows']), 2)
        self.assertTrue(all(row['candidate_for'] == ['Silver Kite', 'Silver Kte'] for row in result['rows']))
        self.assertIn('candidates are included so plans can cover each or ask', FAMILY_INSTRUCTION)
