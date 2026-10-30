"""Full cohort facts survive count collection, partial reads and writer compaction."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence, cohort_review_evidence
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.families.cohort_activity import activity_fields
from elbow_helper.agent.families.core import family_tool
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition
from agent.engine.test_agent_plan_flow import _context, _plan
from agent.engine.test_planner_fix2_evidence import WriterModel, unpack


def cohort(complete=True):
    rows = []
    for i in range(203):
        rows.append({'member_id': 1000 + i if i < 183 else None, 'member_name': f'Member {i}',
            'membership_status': 'present' if i < 183 else 'unlinked',
            'accounts': [{'player_tag': f'#P{i}', 'player_name': f'Account {i}',
                          'clan_code': 'BES', 'donations': i}], 'donations': i,
            'war_activity': {'status': 'observed', 'wars_observed': 3,
                             'attacks_used': 6, 'totals_complete': True},
            'discord_activity': {'status': 'unlinked' if i >= 183 else
                             'no_record' if complete else 'source_unavailable',
                             'last_active_day': None, 'active_streak_days': None, 'lifetime_messages': None}})
    activity_fields(rows)
    return {'basis': 'clan_rosters', 'rows': rows, 'member_count': 183,
        'activity_coverage': {'discord_activity_coverage': {'source_reads_complete': complete,
            'members_checked': 183 if complete else 80, 'members_not_checked': 0 if complete else 103}}}


class CohortWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_uses_retained_table_metadata_and_preserves_authorization_failures(self):
        context = _context()
        rows = [{'player_tag': f'#P{i}', 'player_name': f'Selected account {i}'} for i in range(120)]
        tool = family_tool('read_member_accounts', 'Read selected accounts.', {},
                           AsyncMock(return_value={'rows': rows, 'count_complete': False}))
        page = await tool.handler(context, {'limit': 1})
        evidence = {'accounts': page}
        restored = await cohort_writer_evidence(context, evidence)
        review = cohort_review_evidence(evidence, restored)['accounts']
        self.assertNotIn('rows', review)
        self.assertEqual(review['retained_table']['total_rows'], 120)
        self.assertIn('player_tag', review['retained_table']['columns'])
        self.assertFalse(review['count_complete'])
        self.assertEqual(len(page['rows']), 1)
        self.assertEqual(len(unpack(restored)['accounts']['rows']), 120)
        with patch('elbow_helper.agent.engine.cohort_evidence.authorize_report',
                   AsyncMock(return_value=False)):
            revoked = await cohort_writer_evidence(context, evidence)
        denied_review = cohort_review_evidence(evidence, revoked)['accounts']
        self.assertEqual(denied_review['cause'], 'report_unavailable')
        self.assertNotIn('retained_table', denied_review)
        self.assertNotIn('rows', denied_review)

    def test_review_keeps_discussion_content_for_semantic_source_selection(self):
        evidence = {'discussion': {'family': 'read_conversation_evidence', 'complete_snapshot': True,
            'total_rows': 1, 'rows': [{'message_id': 401, 'content': 'The revised purpose was accepted.'}]}}
        self.assertEqual(cohort_review_evidence(evidence, evidence), evidence)

    async def test_evicted_snapshot_is_reauthorized_with_its_retained_report_and_sources(self):
        context = _context()
        rows = [{'player_tag': '#P0', 'player_name': 'Synthetic player'}]
        tool = family_tool('read_clan_rosters', 'Read roster.', {},
                           AsyncMock(return_value={'rows': rows}))
        result = await tool.handler(context, {})
        identifier = result['report_id']
        snapshot = (context.state.reports.pop(identifier),
                    context.state.report_sources.pop(identifier),
                    context.state.report_access_requirements.pop(identifier))
        restored = await cohort_writer_evidence(context, {'roster': result},
                                                snapshots={identifier: snapshot})
        self.assertEqual(restored['roster']['rows'], rows)
        self.assertNotIn(identifier, context.state.reports)
        with patch('elbow_helper.agent.engine.cohort_evidence.authorize_report',
                   AsyncMock(return_value=False)):
            denied = await cohort_writer_evidence(context, {'roster': result},
                                                  snapshots={identifier: snapshot})
        self.assertNotIn('rows', denied['roster'])

    async def test_ranked_snapshot_restores_every_account_and_rechecks_authorization(self):
        context = _context()
        rows = [{'player_tag': f'#P{i}', 'player_name': f'Player {i}',
                 'attacks_used': i, 'attacks_available': i + 1,
                 'counts_complete': False, 'live_status': 'ok',
                 'observed_attacks': None, 'observed_defenses': None} for i in range(200)]
        tool = family_tool('read_ranked_activity', 'Read ranked activity.', {},
                           AsyncMock(return_value={'rows': rows}))
        result = await tool.handler(context, {'limit': 5})
        self.assertEqual(len(result['rows']), 5)
        restored = await cohort_writer_evidence(context, {'ranked': result})
        table = restored['ranked']['rows']
        complete = [dict(zip(table['columns'], row)) for row in table['rows']]
        self.assertEqual([{key: row[key] for key in rows[0]} for row in complete], rows)
        with patch('elbow_helper.agent.engine.cohort_evidence.authorize_report',
                   AsyncMock(return_value=False)):
            denied = await cohort_writer_evidence(context, {'ranked': result})
        self.assertNotIn('rows', denied['ranked'])

    async def flow(self, source):
        context, model = _context(), WriterModel()
        tool = family_tool('read_member_cohort', 'Read member facts.', {
            'basis': {'type': 'string'}}, source)
        hidden = RegisteredAgentTool(AgentToolDefinition('hidden', 'Specialist.', {}), source,
                                     default_visible=False)
        with patch('elbow_helper.agent.engine.service.build_agent_tools',
                   return_value={'read_member_cohort': tool, 'hidden': hidden}):
            flow, _ = AgentService(model)._start_flow(question='Review all member activity.',
                local_context='', context=context, conversation_history='')
        results = await flow.runner.run(_plan([{'id': 'activity', 'capability': 'read_member_cohort',
            'arguments': {'basis': 'clan_rosters', 'view': 'count'}, 'depends_on': []}]))
        flow.completed_results.update(results)
        flow.result_views.update(flow.runner.model_results_for(results))
        self.assertEqual(results['activity']['rows'], [])
        self.assertEqual(results['activity']['total_rows'], 203)
        self.assertTrue(await flow._prepare_answer_session())
        writer = unpack(json.loads(model.writer['prompt'].split('\nAvailable evidence:\n', 1)[1]))
        self.assertFalse(writer.get('truncated'))
        return flow, writer['results']['activity']

    async def test_count_only_full_family_reaches_real_writer_without_files(self):
        source = cohort()
        flow, writer = await self.flow(AsyncMock(return_value=source))
        rows = writer['rows']
        self.assertEqual(len(rows), 203)
        self.assertEqual(sum(r['member_id'] is not None for r in rows), 183)
        self.assertEqual([r['donations'] for r in rows], list(range(203)))
        self.assertTrue(all(r['attacks_used'] == 6 for r in rows))
        self.assertFalse(flow.context.state.attachments)
        self.assertFalse(flow.context.state.proposed_changes)

    async def test_unavailable_store_reaches_writer_without_repeated_reads(self):
        source = AsyncMock(return_value=cohort(False))
        flow, writer = await self.flow(source)
        self.assertEqual(source.await_count, 1)
        self.assertEqual(flow.runner.tool_calls, 1)
        self.assertFalse(writer['activity_coverage']['discord_activity_coverage']['source_reads_complete'])
        self.assertEqual(len(writer['rows']), 203)
        self.assertTrue(all(r['discord_activity_status'] == 'source_unavailable'
                            for r in writer['rows'] if r['member_id'] is not None))

    async def test_revoked_snapshot_never_restores_retained_member_rows(self):
        flow, _ = await self.flow(AsyncMock(return_value=cohort()))
        evidence = flow.result_views
        with patch('elbow_helper.agent.engine.cohort_evidence.authorize_report',
                   AsyncMock(return_value=False)):
            denied = await cohort_writer_evidence(flow.context, evidence)
        self.assertNotIn('rows', denied['activity'])
        self.assertEqual(denied['activity']['cause'], 'report_unavailable')

    async def test_foreign_requester_snapshot_is_not_rehydrated(self):
        flow, _ = await self.flow(AsyncMock(return_value=cohort()))
        context = replace(flow.context, member=SimpleNamespace(id=99))
        denied = await cohort_writer_evidence(context, flow.result_views)
        self.assertNotIn('rows', denied['activity'])
