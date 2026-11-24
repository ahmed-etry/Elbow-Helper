"""Operator records keep synthetic request evidence away from agent datasets."""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
from tests.temp_directory import TemporaryDirectory
import threading
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.conversation.transcripts import TranscriptArchive
from elbow_helper.agent.conversation.delivery import AgentDeliveryMixin
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.engine.models import AgentTurnState, RegisteredAgentTool
from elbow_helper.agent.engine.tool_call import AgentUnavailableError
from elbow_helper.infrastructure.ai import AgentToolDefinition, TextGenerationError
from tests.agent.loop_stub import StubModel, StubSession, call, final, loop_service, without_server_notes
from tests.agent.request_fixtures import flow_context as _context


class OperatorDebugTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "transcripts.sqlite3"
        self.archive = TranscriptArchive(self.path)
        self.addAsyncCleanup(self.archive.drain)
        self.context = replace(_context(), transcript_archive=self.archive)

    async def records(self):
        await self.archive.drain()
        with self.archive.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT record_json FROM operator_debug_records ORDER BY record_id")]

    async def test_loop_round_usage_and_calls_reach_the_record(self):
        tool = RegisteredAgentTool(AgentToolDefinition("read_value", "Synthetic lookup", {
            "type": "object", "properties": {"value": {"type": "integer"}},
            "required": ["value"], "additionalProperties": False,
        }), AsyncMock(return_value={"value": 7, "evidence": "Synthetic restricted evidence"}),
            contract=CapabilityContract(source_scope="request_context"))
        session = StubSession(call("read_value", {"value": 7}), final("Synthetic answer"))
        service = loop_service(StubModel(session))
        service.registry = {"read_value": tool}
        with ExitStack() as stack:
            without_server_notes(stack)
            answer = await service.answer(question="Synthetic question", context=self.context)
        self.assertEqual(answer, "Synthetic answer")
        record, = await self.records()
        self.assertEqual(record["status"], "completed")
        self.assertEqual([round_["usage"] for round_ in record["rounds"]], [
            {"prompt_tokens": 10, "completion_tokens": 2, "prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 6},
            {"prompt_tokens": 20, "completion_tokens": 5, "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 12}])
        self.assertEqual(record["usage"]["rounds"], 2)
        self.assertEqual((record["usage"]["prompt_tokens"], record["usage"]["completion_tokens"]), (30, 7))
        self.assertEqual(record["usage"]["unknown_token_rounds"], 0)
        call_, = record["tool_calls"]
        self.assertEqual((call_["capability"], call_["arguments"], call_["status"]), ("read_value", {"value": 7}, "completed"))

    async def test_loop_provider_error_and_empty_answer_are_an_unavailable_request(self):
        for name, steps, status in (
            ("provider error", (TextGenerationError("Synthetic provider failure"),), "provider_error"),
            ("empty answer", (final("  "),), "failed"),
        ):
            with self.subTest(name), ExitStack() as stack:
                without_server_notes(stack)
                with self.assertRaises(AgentUnavailableError):
                    await loop_service(StubModel(StubSession(*steps))).answer(
                        question="Synthetic question", context=self.context)
        records = await self.records()
        self.assertEqual([record["status"] for record in records], ["provider_error", "failed"])
        self.assertEqual(records[0]["error"], "Synthetic provider failure")
        self.assertEqual(records[0]["rounds"][0]["outcome"], "failed")
        self.assertEqual(records[0]["usage"]["unknown_token_rounds"], 1)

    async def test_loop_empty_text_is_fine_when_a_gif_is_the_reply_but_not_for_a_reaction_alone(self):
        with ExitStack() as stack:
            without_server_notes(stack)
            context = replace(self.context, state=AgentTurnState())
            context.state.gif_url = "https://example.test/synthetic.gif"
            self.assertEqual(await loop_service(StubModel(StubSession(final("")))).answer(
                question="Synthetic question", context=context), "")
            context = replace(self.context, state=AgentTurnState())
            context.state.reactions.emojis.append("👍")
            with self.assertRaises(AgentUnavailableError):
                await loop_service(StubModel(StubSession(final("")))).answer(
                    question="Synthetic question", context=context)

    async def test_loop_without_a_configured_model_is_an_unavailable_request(self):
        with ExitStack() as stack:
            without_server_notes(stack)
            with self.assertRaises(AgentUnavailableError):
                await loop_service(StubModel()).answer(question="Synthetic question", context=self.context)

    async def test_visible_transcript_writes_keep_order_without_holding_delivery(self):
        started, release = threading.Event(), threading.Event()
        original = self.archive.record_request
        def blocked_request(**values):
            started.set()
            release.wait(5)
            original(**values)
        self.archive.enqueue_write(blocked_request, message_id=1, guild_id=3, channel_id=91,
            root_message_id=1, member_id=41, created_at="2026-01-01", content="Synthetic question",
            replied_to_message_id=None)
        surface = AgentDeliveryMixin()
        surface.transcript_archive = self.archive
        try:
            await asyncio.to_thread(started.wait, 1)
            self.assertTrue(started.is_set())
            await asyncio.wait_for(surface._archive_reply(1, 2, "Synthetic answer"), 1)
            self.assertEqual(self.archive.read_page(guild_id=3, channel_id=91, root_message_id=1), ())
        finally:
            release.set()
            await self.archive.drain()
        rows = self.archive.read_page(guild_id=3, channel_id=91, root_message_id=1)
        self.assertEqual(rows[0]["replies"][0]["content"], "Synthetic answer")
