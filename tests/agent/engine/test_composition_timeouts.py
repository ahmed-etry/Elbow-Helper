"""Exercise request clocks and real provider serialization without network calls."""

import asyncio
from contextlib import contextmanager
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.cog import AGENT_REQUEST_TIMEOUT_SECONDS
from elbow_helper.agent.engine.budgets import (
    DELIVERY_TIME_RESERVE_SECONDS, FINAL_ANSWER_TIME_RESERVE_SECONDS,
)
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.engine.models import RegisteredAgentTool
from elbow_helper.agent.engine.routing import ROUTE_INSTRUCTION
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.configuration.roles import CORE
from elbow_helper.infrastructure.ai import AgentToolDefinition
from tests.agent import test_agent_cog as cog_tests
from tests.agent.engine.test_agent_plan_flow import _plan
from tests.agent.engine.test_answer_composition import call, client, provider_response


class RequestClock:
    """Advance the event loop's real timeout machinery without wall-clock waits."""

    def __init__(self):
        self.started = self.now = time.monotonic()

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


class CompositionTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def scenario(self):
        fixture = cog_tests.AgentCogTests()
        fixture.setUp()
        self.addCleanup(fixture.cog.cog_unload)
        member = cog_tests._Member(42, (next(iter(CORE)),))
        message = fixture._real_handler_scenario(member)(member, 501, "<@999> Review the supplied facts.")
        return fixture.cog, message

    async def run_composition(self, *, duration=361, planning_seconds=20,
                              fail=False, recovery_hangs=False):
        cog, message = self.scenario()
        clock = RequestClock()
        requests, cancelled = [], []
        final = "Synthetic assessment completed."
        fallback = "Synthetic concise assessment."
        source = AsyncMock(return_value={"rows": [{"note": "Supplied evidence."}]})
        registry = {"read_sample": RegisteredAgentTool(AgentToolDefinition(
            "read_sample", "Read synthetic facts.", {"type": "object", "properties": {}}),
            source, contract=CapabilityContract(()))}

        def decision(facts):
            return json.dumps({"intent": "Review the supplied facts", "intent_source": "request",
                "discussion_message_ids": [], "settled_choices": [], "open_choices": [],
                "facts": facts, "question": ""})

        async def provider(**options):
            requests.append((clock.elapsed, options))
            if options["messages"][0]["content"] == ROUTE_INSTRUCTION:
                return provider_response("research")
            if options.get("tool_choice") != "none":
                await clock.elapse(planning_seconds)
                return provider_response(calls=[call("submit_request_plan", {
                    **_plan([{"id": "sample", "capability": "read_sample", "arguments": {},
                              "depends_on": []}], effort="max"),
                    "assessment": True, "finish_after_reads": True})])
            if options["reasoning_effort"] == "max":
                try:
                    await clock.elapse(options["timeout"] if fail else duration)
                    return provider_response(decision(final))
                except asyncio.CancelledError:
                    cancelled.append(("composition", clock.elapsed))
                    raise
            try:
                await clock.elapse(FINAL_ANSWER_TIME_RESERVE_SECONDS if recovery_hangs else 5)
                return provider_response(decision(fallback))
            except asyncio.CancelledError:
                cancelled.append(("fallback", clock.elapsed))
                raise

        model = client(provider)
        self.addAsyncCleanup(model.close)
        cog.service = AgentService(model)
        delivered_at = []

        async def deliver(*args, **kwargs):
            await clock.elapse(5)
            delivered_at.append(clock.elapsed)
            return message.reply.return_value

        message.reply.side_effect = deliver
        with clock.installed(), \
             patch("elbow_helper.agent.cog.discord.Member", cog_tests._Member), \
             patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
            await cog.on_message(message)
        source.assert_awaited_once()
        composition = next(options for _, options in requests
                           if options.get("tool_choice") == "none" and options["reasoning_effort"] == "max")
        self.assertEqual(composition["timeout"], AGENT_REQUEST_TIMEOUT_SECONDS - planning_seconds
                         - FINAL_ANSWER_TIME_RESERVE_SECONDS - DELIVERY_TIME_RESERVE_SECONDS)
        self.assertEqual(composition["max_tokens"], 49_152)
        self.assertTrue(all("timeout" not in options for _, options in requests if options is not composition))
        self.assertLessEqual(clock.elapsed, AGENT_REQUEST_TIMEOUT_SECONDS)
        return message, requests, cancelled, delivered_at

    async def test_slow_composition_uses_remaining_request_time_and_delivers(self):
        message, requests, cancelled, delivered_at = await self.run_composition()
        self.assertEqual(message.reply.await_args.args, ("Synthetic assessment completed.",))
        self.assertEqual(len(requests), 3)
        self.assertEqual(cancelled, [])
        self.assertEqual(delivered_at, [386])

    async def test_overlong_composition_stops_with_time_to_generate_and_deliver_fallback(self):
        message, requests, cancelled, delivered_at = await self.run_composition(fail=True)
        self.assertEqual(message.reply.await_args.args, ("Synthetic concise assessment.",))
        self.assertEqual(len(requests), 4)
        self.assertEqual(cancelled, [("composition", 420)])
        self.assertEqual(requests[-1][0], 420)
        self.assertEqual(requests[-1][1]["reasoning_effort"], "none")
        self.assertEqual(delivered_at, [430])

    async def test_composition_timeout_shrinks_when_earlier_work_uses_more_time(self):
        message, _, cancelled, delivered_at = await self.run_composition(
            planning_seconds=140, duration=200)
        self.assertEqual(message.reply.await_args.args, ("Synthetic assessment completed.",))
        self.assertEqual(cancelled, [])
        self.assertEqual(delivered_at, [345])

    async def test_stalled_fallback_is_also_cut_before_delivery_reserve(self):
        _, requests, cancelled, delivered_at = await self.run_composition(fail=True, recovery_hangs=True)
        self.assertEqual(cancelled, [("composition", 420), ("fallback", 465)])
        self.assertTrue(all(started < 465 for started, _ in requests))
        self.assertTrue(delivered_at)
        self.assertLessEqual(max(delivered_at), AGENT_REQUEST_TIMEOUT_SECONDS)

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


if __name__ == "__main__":
    unittest.main()
