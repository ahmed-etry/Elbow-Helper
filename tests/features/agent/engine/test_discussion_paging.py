"""Conversation discovery retains complete chunks and usable source continuations."""

from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.engine.answer_inputs import answer_evidence, read_handles
from elbow_helper.features.agent.engine.budgets import (
    RESEARCH_LIMITS, MAX_EVIDENCE_CHARACTERS, MAX_DISCUSSION_EVIDENCE_CHARACTERS,
)
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.families.core import family_tool
from elbow_helper.features.agent.families.reference_reads import conversation_evidence
from elbow_helper.features.agent.families.model_view import family_model_view
from features.agent.engine.test_agent_plan_flow import _context, _plan
from features.agent.engine.test_answer_composition import client, provider_response, call, writer_payload
from features.agent.research.test_agent_search import _context as history_context
from features.agent.research.test_history_text_filter import history


class DiscussionPagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_writer_can_review_six_chunks_with_followable_source_handles(self):
        source = AsyncMock()

        async def read(context, arguments):
            index = source.await_count
            return {
                'rows': [{'message_id': 7000 - index * 100 - row, 'author_id': 81, 'author': 'Sample Member',
                          'content': 'The earlier allocation was provisional. ' * 4,
                          'content_complete': True, 'attachment_contents_read': False,
                          'source': f'https://discord.com/channels/3/91/{7000 - index * 100 - row}'}
                         for row in range(50)],
                'mode': 'channel_window', 'channel_id': 91,
                'source_next_cursor': f'older-{index}' if index < 6 else None,
                'coverage': {'next_cursor': f'older-{index}' if index < 6 else None,
                             'reached_requested_start': index == 6},
            }

        source.side_effect = read
        tool = family_tool('read_conversation_evidence', 'Read discussion chunks.', {
            'mode': {'type': 'string'}, 'channel_id': {'type': 'integer'},
            'source_cursor': {'type': 'string'},
        }, source)
        registry = {'read_conversation_evidence': tool, 'hidden': replace(tool, default_visible=False)}
        seen = []

        def plan(arguments):
            return {**_plan([{'id': f'chunk{source.await_count}', 'capability': 'read_conversation_evidence',
                             'arguments': arguments, 'depends_on': []}]), 'finish_after_reads': True}

        async def provider(**options):
            if not source.await_count:
                return provider_response(calls=[call('submit_request_plan', plan({
                    'mode': 'channel_window', 'channel_id': 91}))])
            prompt, _, evidence = writer_payload(options)
            self.assertIn('actual discussion', prompt['composition_constraints'])
            seen.append(json.dumps(evidence))
            if source.await_count < 6:
                handles = [value['source_next_arguments'] for value in prompt['read_handles'].values()
                           if 'source_next_arguments' in value]
                self.assertEqual(handles, [{'mode': 'channel_window', 'channel_id': 91,
                                           'source_cursor': f'older-{source.await_count}'}])
                return provider_response(calls=[call('submit_request_plan', plan(handles[0]))])
            return provider_response('The allocation was provisional.')

        model = client(provider)
        try:
            with patch('elbow_helper.features.agent.engine.service.build_agent_tools', return_value=registry):
                flow, _ = AgentService(model)._start_flow(question='Explain the earlier assignment.',
                    local_context='', context=_context(), conversation_history='')
            self.assertEqual(await flow.run(), 'The allocation was provisional.')
            self.assertEqual(source.await_count, 6)
            self.assertEqual(flow.budget.request_limits, RESEARCH_LIMITS)
            self.assertGreater(flow.runner.evidence_characters, MAX_EVIDENCE_CHARACTERS)
            self.assertLess(flow.runner.evidence_characters, MAX_DISCUSSION_EVIDENCE_CHARACTERS)
            self.assertTrue(all('The earlier allocation was provisional.' in item for item in seen))
        finally:
            await model.close()

    async def test_channel_window_keeps_every_speaker_and_unknown_start(self):
        context = history_context()
        rows = history(context, [(1, 'Opening statement.', (), ()),
                                 (2, 'A differently worded proposal.', (), ()),
                                 (3, 'Follow-up conditions.', (), ('sample.png',))])
        result = await conversation_evidence(context, {
            'mode': 'channel_window', 'channel_id': 100, 'author_id': 1,
        })
        self.assertEqual({row['message_id'] for row in result['rows']}, {row.message_id for row in rows})
        self.assertEqual(context.message_search.history_page.await_args.kwargs['after_id'], 0)
        view = family_model_view({**result, 'family': 'read_conversation_evidence'})
        self.assertEqual({row['content'] for row in view['rows']}, {row.content for row in rows})
        self.assertTrue(view['message_content_complete'])
        self.assertFalse(view['attachment_contents_read'])
        self.assertTrue(all('source' in row and 'author_id' in row for row in view['rows']))

    def test_completed_chunk_supersedes_older_source_handle(self):
        first = {'family': 'read_conversation_evidence', 'channel_id': 100, 'mode': 'channel_window',
                 'source_next_cursor': 'older', 'coverage': {'next_cursor': 'older',
                 'reached_requested_start': False}}
        last = {**first, 'source_next_cursor': None,
                'coverage': {'next_cursor': None, 'reached_requested_start': True}}
        self.assertIn('source_next_arguments', read_handles({'first': first})['first'])
        handles = read_handles({'first': first, 'last': last})
        self.assertFalse(any('source_next_arguments' in value for value in handles.values()))
        self.assertNotIn('source_next_cursor', answer_evidence({'first': first})['first'])
