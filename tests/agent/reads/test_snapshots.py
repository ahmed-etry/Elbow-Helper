"""Complete snapshots, explicit discovery, continuation and export boundaries."""
import csv
from io import StringIO
import json
import unittest
from unittest.mock import AsyncMock, patch
from elbow_helper.agent.reads.snapshots import family_tool, FamilyReport, decode_family, saved_slice
from tests.agent.engine.test_agent_plan_flow import _context


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
        with patch('elbow_helper.agent.reads.snapshots.export_report', side_effect=OSError('Synthetic')):
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

    async def _clan_tool(self):
        calls = []
        async def read(context, arguments):
            calls.append(arguments)
            codes = arguments.get('clan_codes', [])
            return {'rows': [{'clan_code': code, 'player_tag': '#' + code + str(index)}
                             for code in codes for index in range(2)],
                    'count': 2 * len(codes), 'metric': 'ass',
                    'clan_results': [{'clan_code': code, 'completed_wars': 7} for code in codes],
                    'ranked_population': 2 * len(codes)}
        properties = {'clan_codes': {'type': 'array', 'items': {'type': 'string'}},
                      'metric': {'type': 'string'}}
        return family_tool('read_synthetic', 'Read synthetic rows.', properties, read), calls

    async def test_saved_report_serves_a_narrower_clan_slice_without_reading_again(self):
        tool, calls = await self._clan_tool()
        context = _context()
        first = await tool.handler(context, {'clan_codes': ['BE1', 'BE4'], 'metric': 'ass'})
        sliced = await tool.handler(context, {'report_id': first['report_id'],
                                              'clan_codes': ['BE1'], 'view': 'rows'})
        self.assertEqual(len(calls), 1)
        self.assertNotIn('error', sliced)
        self.assertEqual([row['clan_code'] for row in sliced['rows']], ['BE1', 'BE1'])
        self.assertEqual(sliced['total_rows'], 2)
        self.assertEqual(sliced['count'], 2)
        self.assertEqual(sliced['clan_results'], [{'clan_code': 'BE1', 'completed_wars': 7}])
        self.assertNotIn('ranked_population', sliced)
        self.assertEqual(sliced['selection']['clan_codes'], ['BE1'])
        self.assertNotEqual(sliced['report_id'], first['report_id'])
        # The original keeps every row, and the slice carries the original's access provenance.
        self.assertEqual(context.state.reports[first['report_id']].manifest()['total_rows'], 4)
        self.assertEqual(context.state.report_sources[sliced['report_id']],
                         context.state.report_sources[first['report_id']])
        self.assertEqual(context.state.report_access_requirements[sliced['report_id']],
                         context.state.report_access_requirements[first['report_id']])
        exported = await tool.handler(context, {'report_id': sliced['report_id'], 'delivery': 'csv'})
        self.assertEqual(exported['artifact']['total_rows'], 2)

    async def test_repeating_the_saved_selection_uses_the_same_report(self):
        reader = AsyncMock(return_value={'rows': [{'member_id': 41}, {'member_id': 42}]})
        tool = family_tool('read_recruitment_pipeline', 'Read recruitment tickets.',
                           {'stage': {'type': 'string'}}, reader)
        for page in ('cursor', 'offset'):
            context = _context()
            first = await tool.handler(context, {'stage': 'pipeline', 'limit': 1})
            paging = {page: first['next_cursor'] if page == 'cursor' else 1}
            again = await tool.handler(context, {'report_id': first['report_id'], 'stage': 'pipeline',
                                                 'limit': 200, **paging})
            self.assertEqual((again['report_id'], again['rows']),
                             (first['report_id'], [{'member_id': 42}]))
            changed = await tool.handler(context, {'report_id': first['report_id'], 'stage': 'accepted', **paging})
            self.assertEqual(changed['cause'], 'invalid_arguments')
        self.assertEqual(reader.await_count, 2)

    async def test_request_beyond_the_saved_rows_reads_fresh_with_the_saved_selection(self):
        tool, calls = await self._clan_tool()
        context = _context()
        first = await tool.handler(context, {'clan_codes': ['BE1'], 'metric': 'ass'})
        wider = await tool.handler(context, {'report_id': first['report_id'], 'clan_codes': ['BE1', 'BEM']})
        self.assertEqual(calls[-1], {'clan_codes': ['BE1', 'BEM'], 'metric': 'ass'})
        self.assertEqual(len(calls), 2)
        self.assertEqual(wider['total_rows'], 4)
        self.assertNotEqual(wider['report_id'], first['report_id'])
        other = await tool.handler(context, {'report_id': first['report_id'], 'metric': 'bonus'})
        self.assertEqual(calls[-1], {'clan_codes': ['BE1'], 'metric': 'bonus'})
        self.assertEqual(other['selection']['metric'], 'bonus')

    async def test_slice_keeps_access_and_continuation_refusals(self):
        tool, calls = await self._clan_tool()
        context = _context()
        first = await tool.handler(context, {'clan_codes': ['BE1', 'BE4'], 'metric': 'ass', 'limit': 1})
        # A cursor or fresh flag cannot be combined with a changed selection.
        for extra in ({'cursor': first['next_cursor']}, {'offset': 1}, {'fresh': True}):
            result = await tool.handler(context, {'report_id': first['report_id'],
                                                  'clan_codes': ['BE1'], **extra})
            self.assertEqual(result['cause'], 'invalid_arguments')
        # Another member's report and a report without provenance never reach a slice.
        context.state.reports[first['report_id']] = FamilyReport(
            first['report_id'], context.guild.id, 'read_synthetic', json.dumps({'rows': [], 'summary': {
                'requester_id': context.member.id + 1, 'selection': {'clan_codes': ['BE1']}}}))
        result = await tool.handler(context, {'report_id': first['report_id'], 'clan_codes': ['BE1']})
        self.assertEqual(result['cause'], 'report_unavailable')
        context.state.report_sources.clear()
        result = await tool.handler(context, {'report_id': first['report_id'], 'clan_codes': ['BE1']})
        self.assertEqual(result['cause'], 'report_unavailable')
        self.assertEqual(len(calls), 1)

    async def test_member_and_account_slices_need_the_column_on_every_row(self):
        async def read(context, arguments):
            return {'rows': [{'member_id': 1, 'player_tag': '#P0'}, {'member_id': 2, 'player_tag': '#Q0'},
                             {'member_id': 3, 'player_tag': '#Y0'}]}
        tool = family_tool('read_synthetic', 'Read synthetic rows.', {
            'member_ids': {'type': 'array', 'items': {'type': 'integer'}},
            'account_tags': {'type': 'array', 'items': {'type': 'string'}}}, read)
        context = _context()
        first = await tool.handler(context, {'member_ids': [1, 2, 3]})
        by_member = await tool.handler(context, {'report_id': first['report_id'], 'member_ids': [2]})
        self.assertEqual([row['member_id'] for row in by_member['rows']], [2])
        first = await tool.handler(context, {'account_tags': ['#P0', '#Q0']})
        by_tag = await tool.handler(context, {'report_id': first['report_id'], 'account_tags': ['q0']})
        self.assertEqual([row['player_tag'] for row in by_tag['rows']], ['#Q0'])
        # A tag the saved selection never held, or a row without the column, is not served from rows.
        saved = json.loads(context.state.reports[first['report_id']].payload)
        self.assertIsNone(saved_slice(saved, {'account_tags': ['#Y0']}))
        self.assertIsNone(saved_slice(saved, {'account_tags': ['not a tag']}))
        saved['rows'][0].pop('player_tag')
        self.assertIsNone(saved_slice(saved, {'account_tags': ['#P0']}))

    def test_cursor_sentence_only_where_the_lookup_is_not_delivered_whole(self):
        from elbow_helper.agent.engine.registry import build_agent_tools
        tools = build_agent_tools()
        sentence = 'Follow next_cursor to completion.'
        for name in ('read_current_war', 'read_donation_history', 'read_hibernation', 'read_promotion_cases'):
            self.assertNotIn(sentence, tools[name].definition.usage_note)
        # Whole only for some results: other results still page.
        self.assertIn(sentence, tools['read_community_reference'].definition.usage_note)
        async def read(context, arguments):
            return {'rows': []}
        self.assertIn(sentence, family_tool('read_synthetic', 'Read.', {}, read).definition.usage_note)

    async def test_report_requires_provenance_and_matching_family(self):
        async def read(context, arguments):
            return {'rows': [{'count': 1}]}
        tool = family_tool('read_synthetic', 'Read synthetic rows.', {}, read)
        context = _context()
        first = await tool.handler(context, {})
        context.state.report_sources.clear()
        self.assertIn('error', await tool.handler(context, {'report_id': first['report_id']}))
