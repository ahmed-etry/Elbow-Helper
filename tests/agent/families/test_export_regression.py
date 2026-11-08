"""Read exported artifacts back through the complete family-tool delivery path."""

import csv
from io import BytesIO, StringIO
import json
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from elbow_helper.agent.tool_support.core import family_tool
from agent.engine.test_agent_plan_flow import _context


class ExportRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_xlsx_snapshots_reuse_the_existing_file_before_zip_generation(self):
        from elbow_helper.agent.tool_support import core
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
