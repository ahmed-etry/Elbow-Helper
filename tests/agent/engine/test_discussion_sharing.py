"""Writer discussion sharing preserves each message, version and read scope."""
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.cohort_evidence import share_discussion_rows, cohort_writer_evidence
from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.families.core import family_tool
from elbow_helper.agent.engine.grounding import messages_from
from agent.engine.test_agent_plan_flow import _context
from agent.engine.test_writer_tables import expand


def discussion(rows, *, after=None, complete=False, channel=91):
    return {'family': 'read_conversation_evidence', 'mode': 'channel_window',
        'channel_id': channel, 'rows': rows, 'total_rows': len(rows),
        'complete_snapshot': True, 'coverage': {'requested_after': after,
            'requested_before': '2025-05-10T00:00:00+00:00',
            'reached_requested_start': complete},
        'selection': {'mode': 'channel_window', 'channel_id': channel, 'after': after}}


class DiscussionSharingTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{'message_id': i, 'author_id': 52, 'content': 'Observation ' + str(i),
            'source': f'https://discord.com/channels/1/91/{i}',
            'timestamp': f'2025-05-{i:02d}T00:00:00+00:00'} for i in range(1, 16)]

    def test_overlapping_windows_keep_all_unique_messages_and_each_read_boundary(self):
        original = {'wide': discussion(self.rows[:12]),
            'dated': discussion(self.rows[7:], after='2025-05-08T00:00:00+00:00', complete=True)}
        untouched = deepcopy(original)
        result = share_discussion_rows(original)
        self.assertEqual(original, untouched)
        self.assertEqual(list(result), ['wide'])
        visible = expand(result['wide'])
        self.assertEqual(visible['rows'], self.rows)
        self.assertEqual(visible['total_rows'], 15)
        self.assertEqual(set(visible['source_reads']), set(original))
        by_id = {row['message_id']: row for row in visible['rows']}
        for identity, source in original.items():
            metadata = dict(visible['source_reads'][identity])
            reconstructed = [by_id[i] for i in metadata.pop('message_ids')]
            self.assertEqual({**metadata, 'rows': reconstructed}, source)
        self.assertEqual(share_discussion_rows(result), result)

    def test_conflicting_versions_and_distinct_channels_are_not_combined(self):
        changed = {**self.rows[5], 'content': 'A later edited observation.'}
        for other in (discussion([changed]), discussion(self.rows[4:9], channel=92)):
            with self.subTest(other=other):
                original = {'first': discussion(self.rows), 'other': other}
                self.assertEqual(share_discussion_rows(original), original)

    def test_partial_or_failed_snapshots_cannot_become_complete_shared_evidence(self):
        for limits in ({'complete_snapshot': False}, {'total_rows': 40},
                       {'error': 'No longer available', 'cause': 'report_unavailable'}):
            with self.subTest(limits=limits):
                original = {'first': discussion(self.rows),
                    'other': {**discussion(self.rows[4:9]), **limits}}
                self.assertEqual(share_discussion_rows(original), original)

    def test_disjoint_windows_preserve_their_original_layout(self):
        original = {'first': discussion(self.rows[:5]), 'other': discussion(self.rows[5:])}
        self.assertEqual(share_discussion_rows(original), original)

    def test_writer_keeps_observation_dates_and_shared_read_coverage(self):
        original = {'discussion': discussion(self.rows, after='2025-05-01T00:00:00+00:00')}
        projected = answer_evidence(original)
        self.assertEqual(projected['discussion']['coverage'], original['discussion']['coverage'])

    def test_shared_read_metadata_cannot_reintroduce_transport_instructions(self):
        original = {'wide': {**discussion(self.rows), 'report_id': 'opaque locator',
            'instruction': 'INTERNAL_TRANSPORT', 'selection': {'limit': 1}},
            'dated': discussion(self.rows[7:], after='2025-05-08T00:00:00+00:00', complete=True)}
        projected = expand(answer_evidence(share_discussion_rows(original)))['wide']
        header = projected['source_reads']['wide']
        self.assertNotIn('report_id', header)
        self.assertNotIn('instruction', header)
        self.assertNotIn('selection', header)
        self.assertEqual(projected['source_reads']['dated']['coverage']['requested_after'],
                         '2025-05-08T00:00:00+00:00')
        self.assertEqual(header['message_ids'], list(range(1, 16)))


class AuthorizedDiscussionSharingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_retained_pages_share_only_after_each_report_is_authorized(self):
        context = _context()
        rows = [{'message_id': i, 'author_id': 52, 'author': 'Reviewer',
            'content': 'Source observation ' + str(i),
            'source': f'https://discord.com/channels/{context.guild.id}/91/{i}',
            'content_complete': True, 'timestamp': f'2025-05-{i:02d}T00:00:00+00:00'}
            for i in range(1, 16)]
        source = AsyncMock(side_effect=[discussion(rows[:12]), discussion(rows[7:], complete=True)])
        tool = family_tool('read_conversation_evidence', 'Read discussion.', {}, source)
        first = await tool.handler(context, {'limit': 1})
        second = await tool.handler(context, {'limit': 1})
        evidence = {'first': first, 'second': second}
        raw = {key: report.payload for key, report in context.state.reports.items()}
        restored = await cohort_writer_evidence(context, evidence)
        self.assertEqual(len(expand(restored)['first']['rows']), 15)
        self.assertEqual(len(messages_from(restored)), 15)
        self.assertEqual({key: report.payload for key, report in context.state.reports.items()}, raw)
        self.assertEqual(len(first['rows']), 1)
        async def authorize(local, identity):
            return identity != second['report_id']
        with patch('elbow_helper.agent.engine.cohort_evidence.authorize_report',
                   side_effect=authorize):
            denied = await cohort_writer_evidence(context, evidence)
        self.assertEqual(denied['second']['cause'], 'report_unavailable')
        self.assertNotIn('source_reads', denied['first'])
        self.assertEqual(len(messages_from(denied)), 12)
