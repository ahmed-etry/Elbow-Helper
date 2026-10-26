"""Workflow snapshots preserve full cohorts and distinct evidence states."""
from dataclasses import make_dataclass, replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from elbow_helper.features.agent.families import workflow_reads as reads
from features.agent.engine.test_agent_plan_flow import _context


class WorkflowFamilyTests(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_window_filters_typed_events_and_retains_undated_evidence(self):
        rows = [{'ticket_channel_id': 100, 'start_at': '2026-09-05T00:00:00+00:00'},
                {'ticket_channel_id': 200, 'start_at': '2026-08-05T00:00:00+00:00'},
                {'ticket_channel_id': 300}]
        with patch.object(reads, 'active_trial_rows', AsyncMock(return_value={'rows': rows})):
            result = await reads.recruitment(_context(), {'stage': 'active_trials',
                'after': '2026-09-01T00:00:00+00:00', 'before': '2026-10-01T00:00:00+00:00'})
        self.assertEqual([row['ticket_channel_id'] for row in result['rows']], [100, 300])
        self.assertEqual(result['rows'][1]['period_status'], 'unknown_date')
        self.assertFalse(result['period_selection_complete'])
        self.assertFalse(result['history_complete'])

    async def test_default_pipeline_reconciles_ticket_rows_and_retains_independent_source_gaps(self):
        snapshot = AsyncMock(return_value={'error': 'Synthetic unavailable lifecycle', 'cause': 'source_unavailable'})
        active = AsyncMock(return_value={'rows': [{'ticket_channel_id': 100, 'applicant_member_id': 41,
                                                 'timing_status': 'due'}]})
        query = AsyncMock(return_value={'rows': [{'ticket_channel_id': 100,
            'applicant_member_id': 41, 'trial_ended_at': 200}]})
        with patch.object(reads, 'complete_snapshot', snapshot), patch.object(reads, 'active_trial_rows', active), patch.object(reads, 'query_context', query):
            result = await reads.recruitment(_context(), {})
        self.assertEqual(len(result['rows']), 1)
        row = result['rows'][0]
        self.assertEqual(row['applicant_member_id'], 41)
        self.assertEqual(len(row['timeline']), 3)
        self.assertIsNone(row['acceptance_decision'])
        self.assertIsNone(row['finalized'])
        self.assertEqual(result['missing_sources'], [{'stage': 'lifecycle', 'cause': 'source_unavailable'}])

    async def test_hibernation_batches_every_linked_battle_log_within_endpoint_limit(self):
        context = _context()
        context.guild.get_member = lambda identifier: SimpleNamespace(id=identifier, display_name='Synthetic', roles=[], joined_at=None)
        alphabet = '0289PYLQGRJCUV'
        tags = ['#P' + alphabet[index // len(alphabet)] + alphabet[index % len(alphabet)] for index in range(21)]
        context = replace(context, account_links=SimpleNamespace(get_links_for_members=lambda ids:
            {41: [{'player_tag': tag} for tag in tags]}))
        async def api(context, arguments):
            self.assertLessEqual(len(arguments['tags']), 20)
            return {'items': [{'tag': tag, 'status': 'ok', 'data': {'items': []}} for tag in arguments['tags']]}
        source = {'rows': [{'member_id': 41, 'started_at': '2020-01-01T00:00:00+00:00',
                            'started_ts': 1577836800, 'start_time_status': 'known'}]}
        with patch.object(reads, 'complete_snapshot', AsyncMock(return_value=source)), \
                patch.object(reads, 'read_clash', AsyncMock(side_effect=api)) as mocked:
            result = await reads.hibernation(context, {'include_activity': True, 'activity_kinds': ['ranked']})
        self.assertEqual(mocked.await_count, 2)
        self.assertEqual(len(result['rows'][0]['accounts']), 21)
        self.assertTrue(all(row['profile_status'] == 'ok' for row in result['rows'][0]['accounts']))

    async def test_active_trial_snapshot_covers_entries_beyond_the_visible_first_page(self):
        context = _context()
        Row = make_dataclass('Trial', [('ticket_channel_id',int),('applicant_member_id',int),('timing_status',str)])
        trials = tuple(Row(index+100,index+1000,'overdue') for index in range(32))
        async def source(context, arguments):
            context.state.reports['source'] = SimpleNamespace(report_id='source',snapshot=SimpleNamespace(trials=trials))
            return {'report_id':'source','trials':[{'ticket_channel_id':100}], 'next_offset':1}
        result = await reads.complete_snapshot(context, source, {}, 'trials')
        self.assertEqual(len(result['rows']),32)
        self.assertEqual(result['rows'][-1]['applicant_member_id'],1031)
        self.assertNotIn('source',context.state.reports)

    async def test_trial_end_followup_never_invents_a_new_acceptance_or_finalization(self):
        context = _context()
        context = replace(context, bot=SimpleNamespace(paths=SimpleNamespace()))
        query = AsyncMock(return_value={'rows':[{'ticket_channel_id':100,'trial_ended_at':200,
                           'content':'accept or decline words are irrelevant'}]})
        with patch.object(reads,'query_context',query):
            result = await reads.recruitment(context, {'stage':'trial_followups'})
        self.assertEqual(result['rows'][0]['workflow_status'],'trial_ended_followup_recorded')
        self.assertIn('unverified',result['rows'][0]['followup_delivery_status'])
        self.assertIsNone(result['rows'][0]['acceptance_decision'])
        self.assertIsNone(result['rows'][0]['finalized'])
        self.assertIsNone(query.call_args.kwargs['max_rows'])

    async def test_trial_account_projection_keeps_missing_profiles_unknown(self):
        from elbow_helper.features.agent.families import identity_reads
        source = {'rows':[{'applicant_member_id':41,'ticket_channel_id':100}]}
        accounts = {'rows':[{'member_id':41,'player_tag':'#P0','townHallLevel':None,
                            'profile_status':'unavailable'}]}
        with patch.object(reads,'active_trial_rows',AsyncMock(return_value=source)), \
                patch.object(identity_reads,'member_accounts',AsyncMock(return_value=accounts)):
            result = await reads.recruitment(_context(),{'stage':'active_trials','include_accounts':True})
        self.assertIsNone(result['rows'][0]['accounts'][0]['townHallLevel'])
        self.assertEqual(result['rows'][0]['accounts'][0]['profile_status'],'unavailable')

    async def test_hibernation_unknown_start_does_not_become_zero_activity(self):
        context = _context()
        context.guild.get_member = lambda identifier: SimpleNamespace(id=identifier,display_name='Synthetic',roles=[],joined_at=None)
        context = replace(context, account_links=SimpleNamespace(get_links_for_members=lambda ids:{41:[{'player_tag':'#P0'}]}))
        result = {'rows':[{'member_id':41,'started_at':None,'started_ts':None,'start_time_status':'unavailable'}]}
        with patch.object(reads,'complete_snapshot',AsyncMock(return_value=result)), \
             patch.object(reads,'read_clash',AsyncMock()) as api:
            result = await reads.hibernation(context, {'include_activity':True})
        row = result['rows'][0]
        self.assertIsNone(row['joined_at'])
        self.assertIsNone(row['accounts'][0]['observed_ranked_attacks_since_start'])
        api.assert_not_awaited()

    async def test_promotion_keeps_intake_routing_and_response_separate(self):
        from elbow_helper.features.examination.queries import ExaminationCaseSnapshot, ExaminationCaseStatus
        case = ExaminationCaseStatus(100, 41, 'known', 'clan_promo', 'complete', 'required',
                                     'routed', 'initial', 'not_recorded', 'awaiting_response')
        source = ExaminationCaseSnapshot('2026-10-01T00:00:00+00:00', 1, 0, (case,))
        queries = SimpleNamespace(case_registrations=lambda: (SimpleNamespace(ticket_channel_id=100),),
                                  case_snapshot=lambda **kwargs: source)
        result = await reads.promotion(replace(_context(), examination_queries=queries), {})
        row = result['rows'][0]
        self.assertEqual(row['routing_status'],'routed')
        self.assertEqual(row['response_status'],'not_recorded')
        self.assertNotIn('accepted',row)
