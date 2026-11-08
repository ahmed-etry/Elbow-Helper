"""Complete snapshots, explicit discovery, continuation and export boundaries."""
import csv
from io import StringIO
import json
import unittest
from unittest.mock import patch
from elbow_helper.agent.tool_support.core import family_tool, FamilyReport, decode_family
from agent.engine.test_agent_plan_flow import _context


class FamilyReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_keeps_large_tables_internal_and_explicit_export_has_every_row(self):
        for size in (501,):
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
        with patch('elbow_helper.agent.tool_support.core.export_report', side_effect=OSError('Synthetic')):
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
