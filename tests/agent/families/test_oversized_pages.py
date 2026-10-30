"""Rich complete reports reach normal paging before storage rejects them."""
import csv
from io import StringIO
import json
import unittest
from unittest.mock import AsyncMock, patch
from base64 import urlsafe_b64encode
import zlib

from elbow_helper.agent.families.core import FamilyReport, decode_family, family_tool
from elbow_helper.agent.conversation.codec import encode_conversation, decode_conversation
from elbow_helper.agent.conversation.state import Conversation
from elbow_helper.agent.reports.base import MAX_REPORT_PAYLOAD_BYTES
from agent.engine.test_agent_plan_flow import _context


class OversizedPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_over_old_budget_returns_all_rows_by_cursor_and_export(self):
        rows = [{'member_id': index, 'profile': 'Synthetic metadata ' * 100} for index in range(650)]
        self.assertGreater(len(json.dumps(rows).encode()), MAX_REPORT_PAYLOAD_BYTES)
        reader = AsyncMock(return_value={'rows': rows})
        tool = family_tool('read_synthetic', 'Read synthetic records.', {}, reader)
        context = _context()
        first = page = await tool.handler(context, {})
        self.assertNotIn('error', first)
        self.assertTrue(first['next_cursor'])
        observed = []
        while True:
            observed.extend(page['rows'])
            self.assertTrue(page['complete_snapshot'])
            if not page['next_cursor']:
                break
            page = await tool.handler(context, {'report_id': first['report_id'], 'cursor': page['next_cursor']})
        self.assertEqual(observed, rows)
        exported = await tool.handler(context, {'report_id': first['report_id'], 'delivery': 'csv', 'view': 'count'})
        self.assertEqual(exported['artifact_status'], 'complete')
        values = list(csv.DictReader(StringIO(context.state.attachments[0].data.decode('utf-8-sig'))))
        self.assertEqual([int(row['member_id']) for row in values], list(range(650)))
        self.assertEqual(values[-1]['profile'], rows[-1]['profile'])
        report = context.state.reports[first['report_id']]
        restored = decode_family(report.storage_payload(), context.guild.id)
        self.assertEqual(restored.payload, report.payload)
        self.assertEqual(restored.cursor_offset(first['next_cursor']), first['next_offset'])
        conversation = Conversation(context.guild.id, context.source_message.channel.id,
            reports=dict(context.state.reports), report_sources=dict(context.state.report_sources),
            report_access_requirements=dict(context.state.report_access_requirements))
        snapshot = encode_conversation(conversation, root_message_id=71, revision=0)
        reloaded = decode_conversation(snapshot)
        saved = reloaded.reports[report.report_id]
        self.assertEqual(saved.payload, report.payload)
        self.assertEqual(saved.cursor_offset(first['next_cursor']), first['next_offset'])
        self.assertEqual(reloaded.report_sources, conversation.report_sources)
        self.assertLess(report.retained_bytes, MAX_REPORT_PAYLOAD_BYTES)
        reader.assert_awaited_once()

    async def test_actual_capacity_failure_keeps_prior_snapshots_atomic(self):
        reader = AsyncMock(return_value={'rows': [{'value': 1}]})
        tool = family_tool('read_synthetic', 'Read synthetic records.', {}, reader)
        context = _context()
        first = await tool.handler(context, {})
        retained = dict(context.state.reports)
        for target in ('elbow_helper.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES',
                       'elbow_helper.agent.families.core.MAX_FAMILY_SNAPSHOT_BYTES'):
            with self.subTest(target=target), patch(target, 1):
                result = await tool.handler(context, {})
                self.assertEqual(result['cause'], 'capacity')
                self.assertEqual(context.state.reports, retained)
        page = await tool.handler(context, {'report_id': first['report_id']})
        self.assertEqual(page['rows'], [{'value': 1}])

    def test_legacy_payload_decodes_and_corrupt_or_expanding_storage_is_rejected(self):
        row = {'report_id': 'synthetic', 'guild_id': 3, 'family': 'read_synthetic',
               'payload': json.dumps({'rows': [{'value': 1}], 'summary': {}})}
        self.assertIsInstance(decode_family(row, 3), FamilyReport)
        corrupt = ['z1:invalid', 'z1:' + urlsafe_b64encode(zlib.compress(b'x' * 300)).decode()]
        with patch('elbow_helper.agent.families.core.MAX_FAMILY_SNAPSHOT_BYTES', 200):
            for payload in corrupt:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    decode_family({**row, 'payload': payload}, 3)

