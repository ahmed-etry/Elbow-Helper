"""Exercise the request deadline without waiting on the wall clock."""

import asyncio
from contextlib import contextmanager
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.cog import AGENT_REQUEST_TIMEOUT_SECONDS
from elbow_helper.configuration.roles import CORE
from tests.agent import test_agent_cog as cog_tests


class RequestClock:
    """Advance the event loop's real timeout machinery without wall-clock waits."""

    def __init__(self):
        # A whole-second origin keeps every sum and difference exact; the machine's own
        # clock reads in milliseconds, so deadlines computed from it differ in the last bit.
        self.started = self.now = 1_000_000.0

    @property
    def elapsed(self):
        return self.now - self.started

    @contextmanager
    def installed(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "time", side_effect=lambda: self.now), \
             patch.object(loop, "slow_callback_duration", float("inf")), \
             patch("time.monotonic", side_effect=lambda: self.now):
            yield

    async def elapse(self, seconds):
        self.now += seconds
        # Due timers are queued behind the first resumption of sleep(0).
        for _ in range(3):
            await asyncio.sleep(0)


class RequestDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def scenario(self):
        fixture = cog_tests.AgentCogTests()
        fixture.setUp()
        self.addCleanup(fixture.cog.cog_unload)
        member = cog_tests._Member(42, (next(iter(CORE)),))
        message = fixture._real_handler_scenario(member)(member, 501, "<@999> Review the supplied facts.")
        return fixture.cog, message

    async def test_request_envelope_cancels_work_at_its_deadline(self):
        cog, message = self.scenario()
        clock = RequestClock()
        cancelled = []

        async def stalled(*args, **kwargs):
            try:
                await clock.elapse(AGENT_REQUEST_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                cancelled.append(clock.elapsed)
                raise

        cog._answer = AsyncMock(side_effect=stalled)
        cog._recover_stopped_request = AsyncMock()
        with clock.installed(), patch("elbow_helper.agent.cog.discord.Member", cog_tests._Member):
            await cog.on_message(message)
        self.assertEqual(cancelled, [AGENT_REQUEST_TIMEOUT_SECONDS])
        self.assertEqual(clock.elapsed, AGENT_REQUEST_TIMEOUT_SECONDS)
        cog._recover_stopped_request.assert_awaited_once()
