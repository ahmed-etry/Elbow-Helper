"""Complete snapshots, explicit discovery, continuation and export boundaries."""
import csv
from io import StringIO
import json
import unittest
from unittest.mock import Mock, patch
from dataclasses import replace
from elbow_helper.agent.families.core import family_tool, FamilyReport, decode_family
from elbow_helper.agent.families.catalogue import discovery_tool
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.infrastructure.ai import AgentToolDefinition
from agent.engine.test_agent_plan_flow import _context


class FamilyReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_keeps_large_tables_internal_and_explicit_export_has_every_row(self):
        for size in (230, 620):
            async def read(context, arguments):
                return {'rows': [{'count': value} for value in range(size)]}
            tool = family_tool('read_synthetic', 'Read synthetic rows.', {}, read)
            context = _context()
            result = await tool.handler(context, {})
            self.assertEqual(result['total_rows'], size)
            self.assertEqual(len(result['rows']), min(size, 500))
            self.assertEqual(result['resolved_inputs'], {})
            if size > 500:
                self.assertFalse(context.state.attachments)
                result = await tool.handler(context, {'report_id': result['report_id'], 'delivery': 'csv'})
                self.assertEqual(result['artifact']['total_rows'], size)
                exported = list(csv.reader(StringIO(context.state.attachments[0].data.decode('utf-8-sig'))))
                self.assertEqual(len(exported), size + 1)
                continuation = await tool.handler(context, {'report_id': result['report_id'],
                    'cursor': result['next_cursor']})
                self.assertEqual(continuation['rows'][0]['count'], 500)
                self.assertIsNone(continuation['next_cursor'])

    async def test_export_failure_preserves_rows_and_the_complete_snapshot(self):
        async def read(context, arguments):
            return {'rows': [{'count': 1}]}
        tool = family_tool('read_synthetic', 'Read synthetic rows.', {}, read)
        context = _context()
        with patch('elbow_helper.agent.families.core.export_report', side_effect=OSError('Synthetic')):
            result = await tool.handler(context, {'delivery': 'csv'})
        self.assertEqual(result['rows'], [{'count': 1}])
        self.assertEqual(result['artifact_status'], 'unavailable')
        self.assertIn(result['report_id'], context.state.reports)

    async def test_continuation_and_export_share_the_complete_immutable_snapshot(self):
        calls = []
        async def read(context, arguments):
            calls.append(arguments)
            return {'rows': [{'name': '=Synthetic', 'count': index} for index in range(7)]}
        tool = family_tool('read_synthetic', 'Read synthetic rows.', {}, read)
        context = _context()
        first = await tool.handler(context, {'limit': 2})
        self.assertEqual(first['total_rows'], 7)
        self.assertEqual(first['next_offset'], 2)
        second = await tool.handler(context, {'report_id': first['report_id'], 'offset': 2,
                                              'limit': 2, 'delivery': 'csv'})
        self.assertEqual(second['rows'][0]['count'], 2)
        self.assertEqual(len(calls), 1)
        exported = list(csv.reader(StringIO(context.state.attachments[0].data.decode('utf-8-sig'))))
        self.assertEqual(len(exported), 8)
        self.assertEqual(exported[-1][1], '6')
        self.assertEqual(exported[1][0], "'=Synthetic")
        report = context.state.reports[first['report_id']]
        restored = decode_family({'kind': 'family', **report.storage_payload()}, context.guild.id)
        self.assertEqual(restored.page(), report.page())
        with self.assertRaises(ValueError):
            decode_family(report.storage_payload(), context.guild.id + 1)
        self.assertIsInstance(restored, FamilyReport)
        json.loads(restored.payload)['rows'].clear()
        self.assertEqual(restored.manifest()['total_rows'], 7)

    async def test_report_requires_provenance_and_matching_family(self):
        async def read(context, arguments):
            return {'rows': [{'count': 1}]}
        tool = family_tool('read_synthetic', 'Read synthetic rows.', {}, read)
        context = _context()
        first = await tool.handler(context, {})
        context.state.report_sources.clear()
        self.assertIn('error', await tool.handler(context, {'report_id': first['report_id']}))

    async def test_discovery_only_installs_checked_definitions_without_running_them(self):
        execute = Mock()
        tool = RegisteredAgentTool(AgentToolDefinition('synthetic', 'Inspect synthetic state.',
                       {'type': 'object', 'properties': {}}), execute,
                       contract=CapabilityContract(), default_visible=False, specialist_group='records')
        active, changed = {}, Mock()
        available = {'synthetic': tool, 'query_bot_data': replace(tool,
                      definition=replace(tool.definition, name='query_bot_data'))}
        discovery = discovery_tool(available, active, changed)
        result = await discovery.handler(_context(), {'group': 'records', 'intent': 'inspect'})
        self.assertEqual(set(active), {'synthetic', 'query_bot_data'})
        self.assertIn('synthetic', result['catalogue'])
        self.assertNotIn('data_guide', result)
        execute.assert_not_called()
        changed.assert_called_once()

    async def test_opening_records_adds_the_data_query_with_its_table_guide_for_this_reader(self):
        tool = RegisteredAgentTool(AgentToolDefinition('query_bot_data', 'Query stored data.',
                       {'type': 'object', 'properties': {}}), Mock(),
                       contract=CapabilityContract(), default_visible=False, specialist_group='records')
        guide = Mock(return_value='synthetic guide')
        active = {}
        discovery = discovery_tool({'query_bot_data': tool}, active, Mock(), guide)
        context = _context()
        with patch('elbow_helper.agent.families.catalogue.reader_levels', return_value={'lead'}):
            result = await discovery.handler(context, {'group': 'records', 'intent': 'inspect'})
            other = await discovery.handler(context, {'group': 'events', 'intent': 'inspect'})
        self.assertEqual(set(active), {'query_bot_data'})
        self.assertEqual(result['data_guide'], 'synthetic guide')
        guide.assert_called_once_with({'lead'})
        self.assertNotIn('data_guide', other)

    def test_legacy_sql_and_change_tools_are_not_default_visible(self):
        registry = build_agent_tools()
        self.assertFalse(registry['query_bot_data'].default_visible)
        self.assertFalse(registry['delete_discord_messages'].default_visible)

    async def test_production_default_omits_sql_and_rejects_undiscovered_specialists(self):
        from elbow_helper.agent.engine.service import AgentService
        from elbow_helper.agent.plan.checker import check_plan
        from agent.engine.test_agent_plan_flow import _Model, _Session
        flow, _ = AgentService(_Model(_Session([], [])))._start_flow(
            question='Synthetic request', local_context='', context=_context(), conversation_history='')
        self.assertIn('open_capability_group', flow.registry)
        self.assertNotIn('query_bot_data', flow.registry)
        self.assertLessEqual(len(flow.registry), 17)
        policy = flow.budget.request_limits
        self.assertEqual(policy.rounds - policy.answer_repair_rounds, 4)
        self.assertEqual(policy.answer_repair_rounds, 1)
        checked = check_plan({'goal':'Synthetic', 'effort':'low', 'output':'text', 'steps':[
            {'id':'secret', 'capability':'query_bot_data', 'arguments':{'sql':'SELECT 1'}, 'depends_on':[]}]}, flow.registry)
        self.assertFalse(checked.ok)
