"""Read exported artifacts back through the complete family-tool delivery path."""

import csv
from io import BytesIO, StringIO
import json
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from elbow_helper.agent.families.core import family_tool
from agent.engine.test_agent_plan_flow import _context


class ExportRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_xlsx_snapshots_reuse_the_existing_file_before_zip_generation(self):
        from elbow_helper.agent.families import core
        async def source(context, arguments):
            return {'rows': [{'name': 'Account', 'count': 1}]}
        context = _context()
        tool = family_tool('read_synthetic', 'Read rows.', {}, source)
        with patch.object(core, 'export_report', wraps=core.export_report) as export:
            first = await tool.handler(context, {'delivery': 'xlsx'})
            await tool.handler(context, {'report_id': first['report_id'], 'delivery': 'xlsx'})
            await tool.handler(context, {'delivery': 'xlsx'})
        self.assertEqual(export.call_count, 1)
        self.assertEqual(len(context.state.attachments), 1)

    async def test_large_internal_reads_never_attach_files_but_requested_export_is_complete_once(self):
        async def source(context, arguments):
            return {'rows': [{'name': 'Account ' + str(index), 'details': 'x' * 1000}
                             for index in range(620)]}
        context = _context()
        tool = family_tool('read_synthetic', 'Read large evidence.', {}, source)
        first = await tool.handler(context, {})
        self.assertEqual(first['total_rows'], 620)
        self.assertFalse(context.state.attachments)
        self.assertNotIn('artifact', first)
        await tool.handler(context, {'report_id': first['report_id'], 'cursor': first['next_cursor']})
        self.assertFalse(context.state.attachments)
        requested = await tool.handler(context, {'report_id': first['report_id'], 'delivery': 'csv'})
        await tool.handler(context, {'report_id': first['report_id'], 'delivery': 'csv'})
        await tool.handler(context, {'delivery': 'csv'})
        self.assertEqual(len(context.state.attachments), 1)
        self.assertEqual(requested['artifact_status'], 'complete')
        exported = list(csv.reader(StringIO(context.state.attachments[0].data.decode('utf-8-sig'))))
        self.assertEqual(len(exported), 621)

    async def test_xlsx_delivery_preserves_long_scalar_and_nested_values_losslessly(self):
        for value in ('x' * 32767, 'x' * 32768, 'x' * 40000,
                      '=' + 'x' * 32766, {'evidence': ['x' * 40000]}):
            with self.subTest(length=len(value) if isinstance(value, str) else 'nested'):
                calls = []

                async def source(context, arguments):
                    calls.append(arguments)
                    return {'rows': [{'content': value}]}

                context = _context()
                tool = family_tool('read_conversation_evidence', 'Read synthetic evidence.', {}, source)
                first = await tool.handler(context, {'limit': 1})
                result = await tool.handler(context, {'report_id': first['report_id'], 'delivery': 'xlsx'})
                self.assertEqual(result['artifact_status'], 'complete')
                self.assertIsNone(result['next_cursor'])
                self.assertEqual(len(calls), 1)
                artifact = context.state.attachments[-1]
                if artifact.filename.endswith('.xlsx'):
                    book = load_workbook(BytesIO(artifact.data), read_only=True)
                    try:
                        exported = list(book.active.values)
                    finally:
                        book.close()
                else:
                    self.assertTrue(artifact.filename.endswith('.csv'))
                    self.assertIn('XLSX cell limit', result['artifact_note'])
                    exported = list(csv.reader(StringIO(artifact.data.decode('utf-8-sig'))))
                expected = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, dict) else value
                if expected.startswith('='):
                    expected = "'" + expected
                self.assertEqual(len(exported[1][0]), len(expected))
                self.assertEqual(exported[1][0], expected)
                self.assertEqual(result['artifact']['filename'], artifact.filename)
