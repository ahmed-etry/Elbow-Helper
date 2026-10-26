"""Operator records keep synthetic request evidence away from agent datasets."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.conversation.debug import OperatorDebugRecord
from elbow_helper.features.agent.conversation.transcripts import TranscriptArchive, _schema
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.datasets.catalogue import SOURCES
from elbow_helper.features.agent.engine.capability_contract import CapabilityContract
from elbow_helper.features.agent.engine.service import AgentService, AgentUnavailableError
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentStep, AgentToolDefinition, AgentUsage
from elbow_helper.infrastructure.persistence import SQLiteMigration, run_sqlite_migrations, sqlite_connection, sqlite_transaction
from features.agent.engine.helpers import patch_engine
from features.agent.engine.test_agent_plan_flow import _context, _model_step, _plan, _step, _Session
from features.agent.engine.test_agent_service import _AgentModel


class OperatorDebugTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "transcripts.sqlite3"
        self.archive = TranscriptArchive(self.path)
        self.context = replace(_context(), transcript_archive=self.archive)

    async def records(self):
        await self.archive.drain()
        with self.archive.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT record_json FROM operator_debug_records ORDER BY record_id")]

    async def test_plan_correction_results_private_answer_images_and_round_usage_survive(self):
        invalid = _model_step({"invalid": True})
        valid = _model_step(_plan([_step("lookup", {"value": 7})], effort="high"))
        session = _Session([invalid, valid, AgentStep(
            "Synthetic restricted answer", (), AgentUsage(100, 20, 30, 70))], [])
        tool = RegisteredAgentTool(AgentToolDefinition("read_value", "Synthetic lookup", {
            "type": "object", "properties": {"value": {"type": "integer"}},
            "required": ["value"], "additionalProperties": False,
        }), AsyncMock(return_value={"value": 7, "evidence": "Synthetic restricted evidence"}),
            contract=CapabilityContract(source_scope="request_context"))
        context = replace(self.context, attachment_sources=(self.context.source_message,))
        with (
            patch_engine("build_agent_tools", return_value={"read_value": tool}),
            patch("elbow_helper.features.agent.files.images.request_images", new_callable=AsyncMock,
                  return_value=[type("Image", (), {"label": "Synthetic image"})()]),
        ):
            answer = await AgentService(_AgentModel(session)).answer(
                question="Synthetic question", local_context="", context=context)
        self.assertEqual(answer, "Synthetic restricted answer")
        record, = await self.records()
        self.assertEqual(record["question"], "Synthetic question")
        self.assertEqual(record["image_labels"], ["Synthetic image"])
        self.assertEqual(record["answer"], answer)
        self.assertFalse(record["plans"][0]["check"]["ok"])
        self.assertTrue(record["plans"][1]["check"]["ok"])
        call, = record["tool_calls"]
        self.assertEqual(call["arguments"], {"value": 7})
        self.assertEqual(call["result"]["evidence"], "Synthetic restricted evidence")
        self.assertEqual([round_["effort"] for round_ in record["rounds"]], ["low", "high", "high", "low"])
        self.assertEqual(record["rounds"][0]["phase"], "routing")
        self.assertEqual(record["rounds"][-1]["usage"], {
            "prompt_tokens": 100, "completion_tokens": 20,
            "prompt_cache_hit_tokens": 30, "prompt_cache_miss_tokens": 70})
        self.assertTrue(all(round_["elapsed_ms"] >= 0 for round_ in record["rounds"]))
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["route"], "research")
        self.assertEqual(record["usage"]["rounds"], len(record["rounds"]))
        self.assertGreaterEqual(record["usage"]["prompt_tokens"], 100)
        self.assertIsNone(record["terminal_cause"])
        self.assertEqual(self.archive.read_page(guild_id=3, channel_id=91, root_message_id=71), ())
        self.assertTrue(all("transcripts" not in str(source.path) for source in SOURCES))

    async def test_archive_failure_and_blocked_write_do_not_hold_answer(self):
        started, release = threading.Event(), threading.Event()
        def blocked_write(**values):
            started.set()
            release.wait(5)
            raise OSError("Synthetic archive failure")
        session = _Session([AgentStep("Synthetic answer", (), AgentUsage())], [])
        with patch.object(self.archive, "record_debug", side_effect=blocked_write):
            try:
                answer = await asyncio.wait_for(AgentService(_AgentModel(session)).answer(
                    question="Synthetic question", local_context="", context=self.context), 1)
                self.assertEqual(answer, "Synthetic answer")
                await asyncio.to_thread(started.wait, 1)
                self.assertTrue(started.is_set())
            finally:
                release.set()
            with self.assertLogs("elbow_helper.features.agent.conversation.transcripts", level="ERROR"):
                await self.archive.drain()

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

    async def test_provider_setup_failure_is_archived(self):
        with self.assertRaises(AgentUnavailableError):
            await AgentService(_AgentModel(None)).answer(
                question="Synthetic question", local_context="", context=self.context)
        record, = await self.records()
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "The AI backend is not configured")
        self.assertEqual(record["rounds"][0]["phase"], "routing")
        self.assertEqual(record["rounds"][0]["outcome"], "failed")
        self.assertIsNone(record["rounds"][0]["usage"])

    async def test_cancelled_round_is_archived_without_invented_token_usage(self):
        session = _Session([], [])
        session.advance = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await AgentService(_AgentModel(session)).answer(
                question="Synthetic question", local_context="", context=self.context)
        record, = await self.records()
        self.assertEqual(record["status"], "cancelled")
        self.assertGreaterEqual(record["usage"]["unknown_token_rounds"], 1)
        self.assertIsNone(record["rounds"][-1]["usage"])

    async def test_version_one_migrates_and_only_old_diagnostics_are_pruned(self):
        legacy = Path(self.directory.name) / "legacy.sqlite3"
        with sqlite_connection(legacy) as connection:
            run_sqlite_migrations(connection, (SQLiteMigration(1, "legacy", _schema),), target_version=1)
        archive = TranscriptArchive(legacy)
        archive.record_request(message_id=1, guild_id=3, channel_id=91, root_message_id=1,
                               member_id=41, created_at="2025-01-01", content="Synthetic question",
                               replied_to_message_id=None)
        values = dict(record=OperatorDebugRecord("Synthetic question"), request_message_id=1,
                      guild_id=3, channel_id=91, root_message_id=1, member_id=41)
        archive.record_debug(**values)
        with archive.connect() as connection, sqlite_transaction(connection, immediate=True):
            connection.execute("UPDATE operator_debug_records SET created_at=?",
                               ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(),))
        archive.record_debug(**values)
        archive.prune_debug()
        with archive.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT count(*) FROM operator_debug_records").fetchone()[0], 1)
        self.assertEqual(len(archive.read_page(guild_id=3, channel_id=91, root_message_id=1)), 1)
