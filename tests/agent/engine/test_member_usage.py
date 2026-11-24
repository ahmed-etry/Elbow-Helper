"""A member's recorded turns determine admission across restarts."""

from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.conversation.debug import OperatorDebugRecord
from elbow_helper.agent.conversation.delivery import AgentDeliveryMixin
from elbow_helper.agent.conversation.transcripts import TranscriptArchive
from elbow_helper.agent.loop import service as loop
from elbow_helper.configuration.roles import (
    AGENT_TESTER_ROLE_ID, CORE, LEAD_PLUS, MEMBER_ROLE_ID,
    SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID,
)
from elbow_helper.infrastructure.ai import AgentUsage
from tests.agent.loop_stub import StubModel, StubSession, final, stub_stored_data, without_server_notes
from tests.agent.request_fixtures import member_context
from tests.temp_directory import TemporaryDirectory


class MemberUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_stored_spend_refuses_once_until_enough_ages_out_and_exempts_leadership(self):
        directory = self.enterContext(TemporaryDirectory())
        archive = TranscriptArchive(Path(directory) / "agent.sqlite3")
        self.addAsyncCleanup(archive.drain)
        stack = self.enterContext(ExitStack())
        without_server_notes(stack)
        stack.enter_context(patch.object(loop, "build_command_tools", return_value=({}, {})))
        clock = stack.enter_context(patch.object(loop, "datetime"))
        archive_clock = stack.enter_context(patch("elbow_helper.agent.conversation.transcripts.datetime"))

        def set_time(now):
            clock.now.return_value = archive_clock.now.return_value = now

        def service():
            return loop.LoopService(None, stub_stored_data(), transcript_archive=archive)

        agent = service()

        async def ask(role=MEMBER_ROLE_ID, *, private=False, usage=AgentUsage(20, 5, 8, 12)):
            context = member_context()
            context.member.roles = [SimpleNamespace(id=identity) for identity in (AGENT_TESTER_ROLE_ID, role)]
            context.guild.roles = [context.guild.default_role, *context.member.roles]
            context.source_message.created_at = clock.now.return_value
            context = replace(context, private_pass=private, transcript_archive=None if private else archive)
            agent.model = StubModel(StubSession(final("Synthetic answer.", usage)))
            reply = await agent.answer(question="Synthetic request", context=context)
            return reply, context

        monday = datetime(2026, 11, 23, tzinfo=timezone.utc)
        set_time(monday + timedelta(hours=5))
        # An older archived turn cost 0.15 cents before the peak window.
        archive.record_debug(
            record=OperatorDebugRecord("Synthetic request", usage={"cache_miss_tokens": 10_000}),
            request_message_id=None, guild_id=1, channel_id=100, root_message_id=None, member_id=42,
        )
        set_time(monday + timedelta(hours=6, minutes=15))
        # The private turn costs exactly 3 cents at peak and is saved without its content.
        peak_usage = AgentUsage(100_000, 0, 0, 100_000)
        self.assertEqual((await ask(private=True, usage=peak_usage))[0], "Synthetic answer.")
        with archive.connect() as connection:
            private_record = connection.execute(
                "SELECT record_json FROM operator_debug_records ORDER BY record_id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertNotIn("Synthetic", private_record)

        agent = service()
        tuesday = monday + timedelta(days=1)
        set_time(tuesday + timedelta(hours=4))
        for expected in ("You've hit the daily limit. Try again in 3 h.", ""):
            reply, context = await ask()
            self.assertEqual(reply, expected)
            self.assertTrue(context.state.member_limit_refused)
            self.assertEqual(agent.model.requests, [])

        stop_typing = AsyncMock()
        context = replace(context, stop_typing=stop_typing)
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        await surface.send_response(context.source_message, reply, None, context=context)
        surface._send_response.assert_not_awaited()
        stop_typing.assert_awaited_once()

        for role in sorted(LEAD_PLUS | CORE | {SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID}):
            with self.subTest(role=role), patch.object(
                agent.member_usage, "check", side_effect=AssertionError("Leadership was limited"),
            ):
                reply, context = await ask(role, usage=peak_usage)
                self.assertEqual(reply, "Synthetic answer.")
                self.assertFalse(context.state.member_limit_refused)

        await archive.drain()
        now = tuesday + timedelta(hours=5)
        set_time(now)
        rows = archive.member_usage(1, 42, now - timedelta(days=1), now)
        self.assertEqual([usage["cache_miss_tokens"] for _, usage in rows if usage.get("cache_miss_tokens")],
                         [100_000])
        self.assertEqual((await ask())[0], "")
        set_time(tuesday + timedelta(hours=6, minutes=15))
        self.assertEqual((await ask())[0], "Synthetic answer.")
