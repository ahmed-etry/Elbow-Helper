"""Conversation discovery retains complete chunks and usable source continuations."""

from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.answer_inputs import answer_evidence, read_handles
from elbow_helper.agent.engine.budgets import (
    MAX_DISCUSSION_EVIDENCE_CHARACTERS,
)
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.reads.reference import conversation_evidence
from elbow_helper.agent.reads.evidence import family_model_view
from tests.agent.engine.test_agent_plan_flow import _context, _plan
from tests.agent.engine.test_answer_composition import client, provider_response, call, writer_payload
from tests.agent.research.test_agent_search import _context as history_context
from tests.agent.research.test_history_text_filter import history


class DiscussionPagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_period_larger_than_evidence_limit_reaches_writer_as_notes(self):
        # Lengths and reply indexes from saved 13 Nov messages; no member text.
        # Negative indexes identify distinct sources outside this saved chunk.
        shapes = [(13, -1), (41, -2), (101, -3), (35, 1), (54, 2),
                  (41, None), (22, 3), (4, None), (26, None), (37, None),
                  (20, None), (18, None), (50, None), (41, None), (25, None),
                  (23, None), (57, 13), (13, None), (66, None), (60, 17),
                  (118, None), (34, 19), (18, 18), (7, None), (46, 21),
                  (35, 20), (78, 25), (56, None), (11, None), (32, None),
                  (35, None), (33, None), (64, None), (56, None), (93, None),
                  (119, None), (3, 34), (137, 31), (6, None), (50, None)]
        from datetime import datetime, timedelta, timezone
        from types import SimpleNamespace
        import discord
        from elbow_helper.agent.engine.evidence_delta import CONVERSATION_NOTES_INSTRUCTION
        from elbow_helper.agent.reads.reference import TOOLS
        from elbow_helper.agent.reads.evidence import record_rows
        from elbow_helper.infrastructure.discord.message_search import DiscordHistoryPage, DiscordSearchMessage

        for count in (20, 4000):
            with self.subTest(messages=count):
                start = datetime(2026, 11, 13, 6, 52, tzinfo=timezone.utc)
                rows = []
                for index in range(count):
                    length, reply = shapes[index % len(shapes)]
                    when = start + timedelta(seconds=index + 1)
                    block = index // len(shapes) * len(shapes)
                    replied_at = (start + timedelta(seconds=block + reply + 1) if reply is not None and reply >= 0
                                  else start - timedelta(seconds=block - reply) if reply is not None else None)
                    rows.append(DiscordSearchMessage(discord.utils.time_snowflake(when), 91,
                        180024563344605194 + index % 9, f'Member {index % 9}',
                        ('Synthetic conversation text. ' * (length // 29 + 2))[:length], when.isoformat(),
                        reply_to_message_id=discord.utils.time_snowflake(replied_at) if replied_at else None))
                expected = {row.message_id: row for row in rows}

                async def page(**args):
                    selected = [row for row in reversed(rows)
                                if args['after_id'] < row.message_id < args['before_id']]
                    more = len(selected) > args['limit']
                    selected = selected[:args['limit']]
                    return DiscordHistoryPage(tuple(selected), args['before_id'], args['after_id'], args['limit'],
                        selected[-1].message_id if more else None, not more)

                context = replace(_context(), message_search=SimpleNamespace(history_page=AsyncMock(side_effect=page)))
                members = {context.member.id: context.member}
                members.update({row.author_id: SimpleNamespace(id=row.author_id, display_name=row.author_name)
                                for row in rows})
                context.guild.members = list(members.values())
                context.guild.get_member = members.get
                context.source_message.created_at = start + timedelta(days=1)
                context.source_message.id = discord.utils.time_snowflake(context.source_message.created_at)
                tool = next(t for t in TOOLS if t.definition.name == 'read_conversation_evidence')
                registry = {'read_conversation_evidence': tool, 'hidden': replace(tool, default_visible=False)}
                reviewed, note_calls = set(), []
                citation = rows[-1].message_id

                async def provider(**options):
                    system = options['messages'][0]['content']
                    if system == CONVERSATION_NOTES_INSTRUCTION:
                        data = json.loads(options['messages'][1]['content'])
                        messages = record_rows(data['messages'])
                        origin = datetime.fromisoformat(data['time_origin'])
                        mapped = {row['message_id']: discord.utils.time_snowflake(origin +
                            timedelta(microseconds=row['microseconds_after_origin'])) for row in messages}
                        for row in messages:
                            identifier = mapped[row['message_id']]
                            self.assertNotIn(identifier, reviewed)
                            reviewed.add(identifier)
                            self.assertEqual(row['content'], expected[identifier].content)
                            self.assertEqual(data['authors'][row['author_index']]['author_id'], expected[identifier].author_id)
                            reply = row.get('reply_to_message_id')
                            actual_reply = reply.get('unread_message_id') if isinstance(reply, dict) else mapped.get(reply)
                            self.assertEqual(actual_reply, expected[identifier].reply_to_message_id)
                        note_calls.append(len(messages))
                        return provider_response(json.dumps({'notes': [{'text': 'Synthetic discussion finding.',
                            'message_ids': [messages[0]['message_id'], messages[-1]['message_id']]}]}))
                    if not context.message_search.history_page.await_count:
                        return provider_response(calls=[call('submit_request_plan', {
                            **_plan([{'id': 'chat', 'capability': 'read_conversation_evidence', 'arguments': {
                                'mode': 'channel_window', 'channel_id': 91, 'after': start.isoformat(),
                                'before': context.source_message.created_at.isoformat()}, 'depends_on': []}]),
                            'finish_after_reads': True})])
                    _, _, evidence = writer_payload(options)
                    self.assertEqual(evidence['chat']['coverage']['returned_messages'], count)
                    self.assertTrue(evidence['chat']['coverage']['reached_requested_start'])
                    if count > 20:
                        self.assertEqual(evidence['chat']['rows'], [])
                        self.assertIn('notes', evidence['chat'])
                        self.assertNotIn('Synthetic conversation text.', json.dumps(evidence))
                    else:
                        self.assertEqual(len(record_rows(evidence['chat']['rows'])), count)
                    return provider_response(f'Synthetic discussion summary. [[{citation}]]')

                model = client(provider)
                try:
                    with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
                        flow, usage = AgentService(model)._start_flow(question='Summarize this conversation period.',
                            local_context='', context=context, conversation_history='')
                    answer = await flow.run()
                    self.assertIn(f'https://discord.com/channels/3/91/{citation}', answer)
                    self.assertEqual(usage.attempted_rounds, len(note_calls) + 2)
                    self.assertEqual(usage.reported_rounds, usage.attempted_rounds)
                    self.assertEqual(flow.budget.spent_rounds, usage.attempted_rounds)
                    if count > 20:
                        self.assertEqual(reviewed, set(expected))
                        self.assertGreaterEqual(len(note_calls), 3)
                        self.assertLess(flow.runner.evidence_characters, MAX_DISCUSSION_EVIDENCE_CHARACTERS)
                        self.assertNotIn('Synthetic conversation text.', json.dumps(context.state.evidence))
                        self.assertTrue(all(json.loads(report.payload)['rows'] == []
                                            for report in context.state.reports.values()))
                        self.assertTrue(all(json.loads(snapshot[0].payload)['rows'] == []
                                            for snapshot in flow.runner.report_snapshots.values()))
                    else:
                        self.assertEqual(note_calls, [])
                        self.assertEqual(context.message_search.history_page.await_count, 1)
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
        self.assertTrue(all(row['source'].endswith('/' + str(row['message_id']))
                            and 'author_id' in row for row in view['rows']))
        # The lookup's field lists name what its rows carry, and no field they lack.
        from elbow_helper.agent.engine.result_hints import RESULT_FIELDS
        from elbow_helper.agent.plan.format import REFERENCE_RETURNS
        stated = {name.rstrip('?') for name in RESULT_FIELDS['read_conversation_evidence']['rows'][0]}
        self.assertLessEqual({key for row in view['rows'] for key in row}, stated)
        shape = REFERENCE_RETURNS['read_conversation_evidence']
        self.assertTrue(all(key in view['rows'][0]
                            for key in shape.split('rows:[{')[1].split('}]')[0].split(',')))

    async def test_busy_channel_period_preserves_every_message_across_source_pages(self):
        # The 13 Nov request: a three-hour period of a busy channel, asked once.
        from datetime import datetime, timedelta, timezone
        import discord
        from types import SimpleNamespace
        from elbow_helper.infrastructure.discord.message_search import DiscordHistoryPage, DiscordSearchMessage
        from elbow_helper.agent.reads import reference
        from elbow_helper.agent.research.history import (
            COMPLETE_HISTORY_CHARACTER_BUDGET, HISTORY_MESSAGE_CHARACTER_BUDGET,
        )
        from elbow_helper.agent.engine.budgets import MAX_TOOL_RESULT_CHARACTERS
        from elbow_helper.agent.plan.results import model_result, model_view
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = tuple(DiscordSearchMessage(
            discord.utils.time_snowflake(start + timedelta(seconds=20 * (450 - index))), 91,
            180024563344605194 + index % 9, f"Member {index % 9}", f"lol {index}",
            (start + timedelta(seconds=20 * (450 - index))).isoformat(),
            reply_to_message_id=None if index % 3 else 1570826450028986509)
            for index in range(450))

        async def page(**arguments):
            selected = [row for row in rows if arguments['after_id'] < row.message_id < arguments['before_id']]
            selected = selected[:arguments['limit']]
            more = len(selected) == arguments['limit'] and selected[-1] is not rows[-1]
            cursor = selected[-1].message_id if more else None
            return DiscordHistoryPage(tuple(selected), arguments['before_id'], arguments['after_id'],
                                      arguments['limit'], cursor, cursor is None)

        context = replace(_context(), message_search=SimpleNamespace(history_page=AsyncMock(side_effect=page)))
        tool = next(item for item in reference.TOOLS if item.definition.name == 'read_conversation_evidence')
        result = await tool.handler(context, {
            'mode': 'channel_window', 'channel_id': 91,
            'after': start.isoformat(), 'before': (start + timedelta(hours=3)).isoformat()})
        selected = list(result['rows'])
        self.assertIsNone(result['next_cursor'])
        # The rows budget of a complete read, plus the envelope a result allows.
        self.assertLessEqual(len(json.dumps(model_view(model_result(result)), ensure_ascii=False,
                                            separators=(',', ':'))),
                             COMPLETE_HISTORY_CHARACTER_BUDGET + MAX_TOOL_RESULT_CHARACTERS
                             - HISTORY_MESSAGE_CHARACTER_BUDGET)
        while result['source_next_cursor']:
            result = await tool.handler(context, {
                'mode': 'channel_window', 'channel_id': 91,
                'after': start.isoformat(), 'before': (start + timedelta(hours=3)).isoformat(),
                'source_cursor': result['source_next_cursor']})
            selected.extend(result['rows'])
        self.assertEqual([row['message_id'] for row in selected], [row.message_id for row in rows])
        self.assertTrue(result['coverage']['reached_requested_start'])

    def test_long_channel_history_with_some_replies_is_one_table(self):
        from elbow_helper.agent.plan.results import model_result, model_view
        messages = [{'message_id': 100 + index, 'channel_id': 91, 'channel': 'general', 'author_id': 7,
                     'author': 'Member', 'timestamp': f'2026-01-01T00:00:{index:02d}+00:00',
                     'content': f'text {index}', 'source': f'https://discord.com/channels/1/91/{100 + index}',
                     **({'reply_to_message_id': 100} if index == 4 else {})} for index in range(12)]
        view = model_view(model_result({'channel_id': 91, 'channel': 'general', 'messages': messages,
                                        'coverage': {'next_cursor': None}}))
        table = view['messages']
        self.assertEqual(set(table), {'columns', 'rows'})
        self.assertIn('reply_to_message_id', table['columns'])
        column = table['columns'].index('reply_to_message_id')
        self.assertEqual([row[column] for row in table['rows']], [100 if index == 4 else None for index in range(12)])

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
