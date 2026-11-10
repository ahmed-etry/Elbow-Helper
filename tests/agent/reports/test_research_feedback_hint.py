"""Occasional feedback hints at the final Discord delivery boundary."""

import asyncio
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests.temp_directory import TemporaryDirectory
from elbow_helper.agent.conversation.transcripts import TranscriptArchive
from elbow_helper.agent.persistence.schema import migrations
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.engine.lookup_health import lookup_problem
from elbow_helper.agent.engine.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition
from tests.agent.engine.test_agent_plan_flow import _model_step, _plan, _step

from elbow_helper.agent.actions.contracts import ActionRefused, ChangePreview, PreparedAction
from elbow_helper.agent.actions.preview import preview_text
from elbow_helper.agent.conversation.delivery import AgentDeliveryMixin, MAX_RESPONSE_CHARACTERS, RESEARCH_FEEDBACK_HINT_INTERVAL
from elbow_helper.agent.engine.service import AgentService, AgentUnavailableError
from elbow_helper.agent.engine.models import AgentDelivery
from elbow_helper.agent.conversation.text import DISCORD_MESSAGE_LIMIT, chunk_response
from elbow_helper.agent.conversation.wording import FAILURE_MESSAGE, RESEARCH_FEEDBACK_HINT
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage, TextGenerationError
from tests.agent.engine.helpers import social_step
from tests.agent.engine.test_agent_plan_flow import _context
from tests.agent.engine.test_agent_service import _AgentModel, _AgentSession


class ResearchFeedbackHintTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'agent.sqlite3'
        self.archive = TranscriptArchive(self.path)
        self.context = _context()
        self.context = replace(self.context, bot=SimpleNamespace(user=SimpleNamespace(id=999)))
        message = self.context.source_message
        message.guild = self.context.guild
        message.author = self.context.member
        message.mentions = ()
        message.archive_reply = False
        message.reference = None
        message.reply = AsyncMock(return_value=SimpleNamespace(id=501))
        message.channel.send = AsyncMock(return_value=SimpleNamespace(id=502))
        self.sender = AgentDeliveryMixin()
        self.sender.bot = self.context.bot
        self.sender.transcript_archive = self.archive

    async def deliver(self, text, *, context=None, attachments=()):
        message = self.context.source_message
        message.reply.reset_mock()
        message.channel.send.reset_mock()
        delivery = AgentDelivery()
        context = context or self.context
        context.state.attachments = list(attachments)
        await self.sender.send_response(message, text, None, context=context,
                                        attachments=attachments, delivery=delivery)
        parts = [call.args[0] for call in message.reply.call_args_list]
        parts.extend(call.args[0] for call in message.channel.send.call_args_list)
        self.assertEqual(delivery.generated_parts, [part for part in delivery.text_parts if part])
        return parts, delivery

    async def answer(self, *, route="research", steps=None):
        session = _AgentSession(steps or [AgentStep("Synthetic finding.", (), AgentUsage())])
        return await AgentService(_AgentModel(session, route=route)).answer(
            question="What is the synthetic finding?", local_context="", context=self.context,
        )

    def next_hint_due(self):
        self.context.state.research_answer_completed = True
        with self.archive.connect() as connection:
            connection.execute('INSERT INTO feedback_hint_counter VALUES (1, ?) '
                               'ON CONFLICT(id) DO UPDATE SET answers=excluded.answers',
                               (RESEARCH_FEEDBACK_HINT_INTERVAL - 1,))
            connection.commit()

    async def test_every_fifth_answer_including_plain_chat_gets_exact_final_line(self):
        self.assertEqual(RESEARCH_FEEDBACK_HINT, "-# React \U0001f44e if this answer was off")
        for number in range(1, 11):
            social = number % 2 == 1
            answer = await self.answer(route='social' if social else 'research',
                                       steps=[social_step('Synthetic finding.')] if social else None)
            self.assertEqual(self.context.state.research_answer_completed, not social)
            self.assertNotIn(RESEARCH_FEEDBACK_HINT, answer)
            parts, _ = await self.deliver(answer)
            expected = "Synthetic finding."
            if number % RESEARCH_FEEDBACK_HINT_INTERVAL == 0:
                expected += "\n" + RESEARCH_FEEDBACK_HINT
            self.assertEqual(parts, [expected])

    async def test_plain_chat_can_be_the_fifth_answer(self):
        self.next_hint_due()
        answer = await self.answer(route='social', steps=[social_step('Synthetic banter.')])
        self.assertFalse(self.context.state.research_answer_completed)
        self.assertEqual((await self.deliver(answer))[0], [answer + '\n' + RESEARCH_FEEDBACK_HINT])

    async def test_social_expansion_to_research_is_eligible(self):
        self.next_hint_due()
        answer = await self.answer(route="social", steps=[
            AgentStep("", (AgentToolCall("expand", "load_research_context", "{}"),), AgentUsage()),
            AgentStep("Synthetic finding.", (), AgentUsage()),
        ])
        self.assertTrue(self.context.state.research_answer_completed)
        self.assertEqual((await self.deliver(answer))[0], [answer + "\n" + RESEARCH_FEEDBACK_HINT])

    async def test_provider_failure_clears_eligibility_and_failure_send_has_no_hint(self):
        self.next_hint_due()
        session = _AgentSession([])
        session.advance = AsyncMock(side_effect=TextGenerationError("Synthetic failure"))
        with self.assertRaises(AgentUnavailableError):
            await AgentService(_AgentModel(session)).answer(
                question="Synthetic question", local_context="", context=self.context,
            )
        self.assertFalse(self.context.state.research_answer_completed)
        await self.sender._send_failure(self.context.source_message)
        self.assertEqual(self.context.source_message.reply.call_args.args[0], FAILURE_MESSAGE)


    async def test_previews_private_results_and_followup_repairs_are_ineligible(self):
        self.context.state.feedback_lookup_problem = True
        cases = (
            ("proposed_changes", [object()]), ("preview_reply", "Synthetic preview"),
            ("provider_failed", True), ("terminal_cause", "limit"),
            ("member_limit_refused", True),
            ("outcomes", [SimpleNamespace(visibility="private")]),
            ("scope_audit", replace(self.context.state.scope_audit, offered=True)),
            ("reply_context", {"conversation": {"request_message_id": 1}}),
            ("reply_context", {"author": {"member_id": 999}}),
        )
        for name, value in cases:
            with self.subTest(name=name):
                self.next_hint_due()
                context = replace(self.context, state=replace(self.context.state, **{name: value}))
                self.assertFalse(await self.sender._research_feedback_hint_due(context, "Synthetic answer", ["Synthetic answer"]))
        for changes in ({"private_pass": True}, {"dm_delivery": True}, {"history": (object(),)}):
            with self.subTest(changes=changes):
                self.assertFalse(await self.sender._research_feedback_hint_due(
                    replace(self.context, **changes), "Synthetic answer", ["Synthetic answer"],
                ))

    async def test_long_answer_keeps_original_chunks_and_only_last_gets_hint(self):
        self.next_hint_due()
        text = "First finding.\n" * 160 + "Final finding."
        original = chunk_response(text)
        self.assertGreater(len(original), 1)
        parts, _ = await self.deliver(text)
        self.assertEqual(parts, [*original[:-1], original[-1] + "\n" + RESEARCH_FEEDBACK_HINT])
        self.assertTrue(all(len(part) <= DISCORD_MESSAGE_LIMIT for part in parts))

    async def test_mixed_preview_keeps_hint_out_of_combined_and_split_answers(self):
        action = PreparedAction(
            "synthetic_change", {},
            ChangePreview(("Change synthetic target",), AsyncMock(return_value=True)), AsyncMock(),
        )
        self.context.state.proposed_changes = [action]
        self.context.state.preview_reply = preview_text([action])
        for text in ("Synthetic finding.", "Synthetic finding.\n" * 160):
            for preview_first in (False, True):
                with self.subTest(length=len(text), preview_first=preview_first):
                    self.next_hint_due()
                    self.context.state.preview_first = preview_first
                    parts, _ = await self.deliver(text)
                    self.assertTrue(all(RESEARCH_FEEDBACK_HINT not in part for part in parts))
                    self.assertEqual(self.answer_count(), RESEARCH_FEEDBACK_HINT_INTERVAL - 1)
                    for call in self.context.source_message.reply.call_args_list:
                        if view := call.kwargs.get("view"):
                            view.stop()

    async def test_full_final_chunk_skips_hint_without_an_extra_message(self):
        for text in ("x" * DISCORD_MESSAGE_LIMIT, "x" * (2 * DISCORD_MESSAGE_LIMIT),
                     "x" * (DISCORD_MESSAGE_LIMIT - len(RESEARCH_FEEDBACK_HINT))):
            with self.subTest(length=len(text)):
                self.next_hint_due()
                parts, _ = await self.deliver(text)
                self.assertEqual(parts, chunk_response(text))

    async def test_closed_code_blocks_get_hint_outside_and_open_fences_skip_it(self):
        for fence in ("```", "~~~~"):
            self.next_hint_due()
            text = fence + "python\nprint('synthetic')\n" + fence
            self.assertEqual((await self.deliver(text))[0], [text + "\n" + RESEARCH_FEEDBACK_HINT])
            self.next_hint_due()
            text = fence + "python\nprint('synthetic')"
            self.assertEqual((await self.deliver(text))[0], [text])


    async def test_file_fallback_keeps_original_answer_and_has_no_hint_only_message(self):
        self.next_hint_due()
        text = "x" * (MAX_RESPONSE_CHARACTERS + 1)
        parts, delivery = await self.deliver(text)
        self.assertEqual(parts, [None])
        self.assertEqual(delivery.text_parts, [text])
        file, = self.context.source_message.reply.call_args.kwargs["files"]
        self.assertEqual(file.fp.getvalue().decode("utf-8"), text)

    def answer_count(self):
        with self.archive.connect() as connection:
            return connection.execute('SELECT answers FROM feedback_hint_counter').fetchone()[0]

    async def test_restart_continues_count_and_sqlite_runs_off_event_loop(self):
        for _ in range(4):
            self.assertEqual((await self.deliver('Synthetic answer.'))[0], ['Synthetic answer.'])
        self.sender = AgentDeliveryMixin()
        self.sender.bot = self.context.bot
        self.sender.transcript_archive = TranscriptArchive(self.path)
        main_thread = threading.get_ident()
        original = self.sender.transcript_archive.next_feedback_answer
        def advance():
            self.assertNotEqual(threading.get_ident(), main_thread)
            return original()
        with patch.object(self.sender.transcript_archive, 'next_feedback_answer', side_effect=advance):
            self.assertEqual((await self.deliver('After restart.'))[0],
                             ['After restart.\n' + RESEARCH_FEEDBACK_HINT])
        self.assertEqual(self.answer_count(), 5)

    async def test_migration_from_previous_version_preserves_agent_data(self):
        old_path = Path(self.directory.name) / 'old.sqlite3'
        with closing(sqlite3.connect(old_path)) as connection:
            for migration in migrations()[:-1]:
                migration.apply(connection)
            connection.execute('PRAGMA user_version=8')
            connection.execute("INSERT INTO requests VALUES (1, 2, 3, 1, 4, 'today', 'Synthetic question', NULL, '[]')")
            connection.commit()
        archive = TranscriptArchive(old_path)
        with archive.connect() as connection:
            self.assertIsNone(connection.execute('SELECT answers FROM feedback_hint_counter').fetchone())
        self.assertEqual(await asyncio.to_thread(archive.next_feedback_answer), 1)
        with archive.connect() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 9)
            self.assertEqual(connection.execute('SELECT content FROM requests').fetchone()[0], 'Synthetic question')
        self.assertEqual(TranscriptArchive(old_path).next_feedback_answer(), 2)

    def test_existing_schema_nine_counter_preserves_its_count(self):
        with self.archive.connect() as connection, connection:
            connection.execute('INSERT INTO feedback_hint_counter VALUES (1, 14)')
        archive = TranscriptArchive(self.path)
        self.assertEqual(archive.next_feedback_answer(), 15)
        self.assertEqual(TranscriptArchive(self.path).next_feedback_answer(), 16)

    def test_failed_first_counter_write_can_retry_without_consuming_a_count(self):
        with self.archive.connect() as connection:
            connection.execute("CREATE TRIGGER fail_counter BEFORE INSERT ON feedback_hint_counter "
                               "BEGIN SELECT RAISE(ABORT, 'Synthetic counter failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'Synthetic counter failure'):
            self.archive.next_feedback_answer()
        with self.archive.connect() as connection:
            self.assertIsNone(connection.execute('SELECT answers FROM feedback_hint_counter').fetchone())
            connection.execute('DROP TRIGGER fail_counter')
        self.assertEqual(self.archive.next_feedback_answer(), 1)

    async def test_concurrent_answers_share_one_durable_count(self):
        counts = await asyncio.gather(*(asyncio.to_thread(self.archive.next_feedback_answer) for _ in range(10)))
        self.assertEqual(sorted(counts), list(range(1, 11)))

    async def test_successful_plan_result_with_missing_data_forces_hint(self):
        for payload in ({'missing_sources': [{'source': 'synthetic'}]},
                        {'clan_totals': [{'season_complete': False}]}):
            with self.subTest(payload=payload):
                handler = AsyncMock(return_value=payload)
                registry = {'read_value': RegisteredAgentTool(AgentToolDefinition('read_value', 'Synthetic lookup',
                            {'type': 'object', 'properties': {}}), handler, contract=CapabilityContract(()))}
                with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
                    answer = await self.answer(steps=[_model_step(_plan([_step('first')])),
                                                     AgentStep('Synthetic answer.', (), AgentUsage())])
                handler.assert_awaited_once()
                self.assertTrue(self.context.state.feedback_lookup_problem)
                self.assertEqual((await self.deliver(answer))[0], [answer + '\n' + RESEARCH_FEEDBACK_HINT])

    async def test_plain_answer_text_cannot_trigger_hint(self):
        answer = 'Failed, refused, missing sources, incomplete season, unavailable clan.'
        self.assertEqual((await self.deliver(answer))[0], [answer])
        self.assertEqual(self.answer_count(), 1)

    def test_pagination_alone_is_not_missing_source_data(self):
        page = {'complete': False, 'next_offset': 25, 'flags': {'status': 'partial', 'truncated': False}}
        self.assertFalse(lookup_problem(page))
        self.assertTrue(lookup_problem({**page, 'missing_sources': [{'source': 'synthetic'}]}))
        self.assertTrue(lookup_problem({**page, 'flags': {'status': 'partial', 'truncated': True}}))

    async def test_post_reply_does_not_count_or_get_hint(self):
        self.next_hint_due()
        self.context.state.feedback_lookup_problem = True
        self.context.state.reply_delivery.posts.append({})
        await self.sender.send_post_reply(self.context.source_message, 'Synthetic posted answer.',
                                          None, self.context, None)
        self.assertEqual(self.context.source_message.reply.call_args.args[0], 'Synthetic posted answer.')
        self.assertEqual(self.answer_count(), 4)

    async def test_counter_failure_keeps_answer_and_forced_hint(self):
        for problem in (False, True):
            self.context.state.feedback_lookup_problem = problem
            with (patch.object(self.archive, 'next_feedback_answer', side_effect=sqlite3.OperationalError('Synthetic failure')),
                  self.assertLogs('elbow_helper.agent.conversation.delivery', level='ERROR')):
                parts, _ = await self.deliver('Synthetic answer.')
            self.assertEqual(parts, ['Synthetic answer.' + ('\n' + RESEARCH_FEEDBACK_HINT if problem else '')])

    async def test_structured_lookup_problems_always_get_hint_and_advance_count(self):
        payloads = (
            {'error': 'Synthetic refusal'}, {'flags': {'status': 'refused'}},
            {'flags': {'status': 'failed'}}, {'missing_sources': [{'source': 'synthetic'}]},
            {'clan_totals': [{'season_complete': False}]},
            {'items': [{'status': 'ok'}, {'status': 'unavailable'}]},
            {'coverage': {'signals': [{'source_complete': False}]}},
            {'history_coverage': {'status': 'partial'}},
            {'history_coverage': {'status': 'empty'}},
            {'coverage': {'gaps': [{'after': 1, 'before': 2}]}},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                handler = AsyncMock(return_value=payload)
                await AgentService.execute_tool(name='synthetic_lookup', handler=handler,
                                                arguments={}, context=self.context)
                self.assertTrue(self.context.state.feedback_lookup_problem)
                self.assertEqual((await self.deliver('Synthetic answer.'))[0],
                                 ['Synthetic answer.\n' + RESEARCH_FEEDBACK_HINT])
                self.context.state.feedback_lookup_problem = False
        self.assertEqual(self.answer_count(), len(payloads))
        self.assertFalse(lookup_problem({'rows': [], 'flags': {'status': 'complete'}}))
        self.assertFalse(lookup_problem({'message': 'failed refused missing unavailable', 'rows': []}))

    async def test_tool_refusal_and_timeout_force_hint_after_state_rollback(self):
        for error in (ActionRefused('Synthetic refusal'), TimeoutError('Synthetic timeout')):
            with self.subTest(error=type(error).__name__):
                self.context.state.feedback_lookup_problem = False
                await AgentService.execute_tool(name='synthetic_lookup', handler=AsyncMock(side_effect=error),
                                                arguments={}, context=self.context)
                self.assertTrue(self.context.state.feedback_lookup_problem)
                self.assertEqual((await self.deliver('Synthetic answer.'))[0],
                                 ['Synthetic answer.\n' + RESEARCH_FEEDBACK_HINT])

    async def test_malformed_revision_keeps_hint_after_successful_read(self):
        handler = AsyncMock(return_value={'value': 7})
        registry = {'read_value': RegisteredAgentTool(AgentToolDefinition('read_value', 'Synthetic lookup',
                    {'type': 'object', 'properties': {}}), handler, contract=CapabilityContract(()))}
        broken = AgentStep('', (AgentToolCall('broken', 'unsupported_tool', '{}'),), AgentUsage())
        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
            answer = await self.answer(steps=[_model_step(_plan([_step('first')])), broken,
                                             AgentStep('Synthetic recovered answer.', (), AgentUsage())])
        handler.assert_awaited_once()
        self.assertTrue(self.context.state.feedback_lookup_problem)
        self.assertEqual((await self.deliver(answer))[0], [answer + '\n' + RESEARCH_FEEDBACK_HINT])

    async def test_both_refused_plans_with_no_reads_still_get_hint(self):
        handler = AsyncMock(return_value={'value': 7})
        registry = {'read_value': RegisteredAgentTool(AgentToolDefinition('read_value', 'Synthetic lookup',
                    {'type': 'object', 'properties': {}}), handler, contract=CapabilityContract(()))}
        invalid = {**_plan([_step('first')]), 'output': 'invalid'}
        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
            answer = await self.answer(steps=[_model_step(invalid), _model_step(invalid),
                                             AgentStep('Synthetic answer anyway.', (), AgentUsage())])
        handler.assert_not_awaited()
        self.assertEqual(self.context.state.evidence, [])
        self.assertTrue(self.context.state.feedback_lookup_problem)
        self.assertEqual((await self.deliver(answer))[0], [answer + '\n' + RESEARCH_FEEDBACK_HINT])

    async def test_failed_step_and_invalid_step_force_hint(self):
        for failure in ('invalid', 'exception'):
            with self.subTest(failure=failure):
                self.context.state.feedback_lookup_problem = False
                handler = AsyncMock(side_effect=OSError('Synthetic lookup failure'))
                registry = {'read_value': RegisteredAgentTool(AgentToolDefinition('read_value', 'Synthetic lookup',
                            {'type': 'object', 'properties': {'value': {'type': 'integer'}}}),
                            handler, contract=CapabilityContract(()))}
                plan = _plan([_step('first', {'value': 'invalid'} if failure == 'invalid' else {})])
                with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value=registry):
                    answer = await self.answer(steps=[_model_step(plan), AgentStep('Synthetic answer.', (), AgentUsage())])
                self.assertTrue(self.context.state.feedback_lookup_problem)
                self.assertEqual((await self.deliver(answer))[0], [answer + '\n' + RESEARCH_FEEDBACK_HINT])

    async def test_gif_only_reply_does_not_advance_count(self):
        self.next_hint_due()
        self.context.state.gif_url = 'https://example.com/synthetic.gif'
        self.sender._send_gif = AsyncMock()
        await self.sender.send_response(self.context.source_message, '', None, context=self.context)
        self.sender._send_gif.assert_awaited_once()
        self.assertEqual(self.answer_count(), 4)

    async def test_ineligible_answer_still_excluded_when_lookup_failed(self):
        self.next_hint_due()
        self.context.state.feedback_lookup_problem = True
        for text in (FAILURE_MESSAGE, '', '```python\nunfinished'):
            parts, _ = await self.deliver(text)
            self.assertTrue(all(RESEARCH_FEEDBACK_HINT not in (part or '') for part in parts))
        self.assertEqual(self.answer_count(), 4)
