"""Member allowances through routing, escalation and Discord delivery."""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import (
    AGENT_TESTER_ROLE_ID, CORE, LEAD_PLUS, MEMBER_ROLE_ID,
    SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID,
)
from elbow_helper.agent.conversation.delivery import AgentDeliveryMixin
from elbow_helper.agent.actions.answer_view import PrivateAnswerView
from elbow_helper.agent.engine.member_usage import (
    DAY_SECONDS, HOUR_SECONDS, MemberUsageLimit, MemberUsageLimiter,
)
from tests.agent.engine.helpers import social_step
from tests.agent.request_fixtures import member_context as _context
from elbow_helper.agent.loop import service as loop
from tests.agent.loop_stub import StubModel, StubSession, final, loop_service, without_server_notes


def member_context(role=MEMBER_ROLE_ID, *, guild_id=1, member_id=42):
    context = _context()
    context.member.id = member_id
    context.guild.id = guild_id
    context.member.roles = [SimpleNamespace(id=identity) for identity in (AGENT_TESTER_ROLE_ID, role)]
    context.guild.roles = [context.guild.default_role, *context.member.roles]
    return context


class LoopMemberUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_over_the_hourly_limit_is_refused_with_one_notice_and_banter_counts_separately(self):
        service = loop_service(None)
        with ExitStack() as stack:
            without_server_notes(stack)
            stack.enter_context(patch.object(loop, "build_command_tools", return_value=({}, {})))

            async def ask(*steps, context=None):
                service.model = StubModel(StubSession(*steps))
                context = context or member_context()
                return await service.answer(question="Synthetic request", context=context), context

            for number in range(10):
                self.assertEqual((await ask(final(f"Answer {number}.")))[0], f"Answer {number}.")
            for expected in ("You've hit the hourly limit. Try again in 60 min.", ""):
                reply, context = await ask(final("Never sent."))
                self.assertEqual(reply, expected)
                self.assertTrue(context.state.member_limit_refused)
                self.assertEqual(service.model.requests, [])
            # A banter reply spends the banter allowance and gives the question place back.
            reply, _ = await ask(social_step("Synthetic banter."), context=member_context(member_id=43))
            self.assertEqual(reply, "Synthetic banter.")
            usage = service.member_usage._members[1, 43]
            self.assertEqual((len(usage.banter), len(usage.questions)), (1, 0))
            # Leadership is not counted.
            reply, context = await ask(final("Lead answer."), context=member_context(min(LEAD_PLUS), member_id=44))
            self.assertEqual(reply, "Lead answer.")
            self.assertNotIn((1, 44), service.member_usage._members)


class MemberUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = self.enterContext(patch(
            "elbow_helper.agent.engine.member_usage.monotonic", return_value=100_000.0,
        ))
        stack = self.enterContext(ExitStack())
        without_server_notes(stack)
        stack.enter_context(patch.object(loop, "build_command_tools", return_value=({}, {})))
        self.service = loop_service(None)

    async def ask(self, *, context=None):
        self.service.model = StubModel(StubSession(final("Synthetic answer.")))
        context = context or member_context()
        reply = await self.service.answer(question="Synthetic request", context=context)
        return reply, context

    async def test_thirty_banter_replies_then_one_notice_and_silence_across_channels(self):
        for _ in range(29):
            self.service.member_usage.acquire(1, 42, question=False)
        self.service.model = StubModel(StubSession(social_step("Synthetic banter.")))
        self.assertEqual(await self.service.answer(question="Synthetic request", context=member_context()),
                         "Synthetic banter.")
        for expected in ("You've hit the hourly limit. Try again in 60 min.", "", ""):
            context = member_context()
            context.source_message.channel.id = 200
            reply, context = await self.ask(context=context)
            self.assertEqual(reply, expected)
            self.assertTrue(context.state.member_limit_refused)
            self.assertEqual(self.service.model.requests, [])
        self.assertEqual(len(self.service.member_usage._members[1, 42].banter), 30)

    async def test_forty_daily_questions_across_hours_and_daily_rollover(self):
        for hour in range(4):
            self.clock.return_value = 100_000 + hour * HOUR_SECONDS
            for _ in range(10):
                self.service.member_usage.acquire(1, 42, question=True)
        self.clock.return_value += HOUR_SECONDS
        self.assertEqual((await self.ask())[0],
                         "You've hit the daily limit. Try again in 20 h.")
        self.assertEqual((await self.ask())[0], "")
        self.assertEqual(self.service.model.requests, [])
        self.clock.return_value = 100_000 + DAY_SECONDS
        self.assertEqual((await self.ask())[0], "Synthetic answer.")

    async def test_leadership_including_sleeping_roles_bypasses_both_caps(self):
        for role in sorted(LEAD_PLUS | CORE | {SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID}):
            with self.subTest(role=role):
                # Even existing member usage must not cap a newly promoted leader.
                for _ in range(30):
                    try:
                        self.service.member_usage.acquire(1, 42, question=False)
                    except MemberUsageLimit:
                        pass
                for _ in range(10):
                    try:
                        self.service.member_usage.acquire(1, 42, question=True)
                    except MemberUsageLimit:
                        pass
                with patch.object(self.service.member_usage, "admit",
                                  side_effect=AssertionError("Leadership was counted")):
                    reply, context = await self.ask(context=member_context(role))
                    self.assertTrue(reply)
                    self.assertFalse(context.state.member_limit_refused)

    async def test_rolling_hour_frees_a_slot_and_resets_notice(self):
        for _ in range(10):
            self.service.member_usage.acquire(1, 42, question=True)
        self.clock.return_value += HOUR_SECONDS - 1
        self.assertEqual((await self.ask())[0],
                         "You've hit the hourly limit. Try again in 1 min.")
        self.clock.return_value += 1
        for _ in range(9):
            self.service.member_usage.acquire(1, 42, question=True)
        self.assertEqual((await self.ask())[0], "Synthetic answer.")
        self.assertEqual((await self.ask())[0],
                         "You've hit the hourly limit. Try again in 60 min.")

    async def test_guilds_and_members_have_independent_allowances(self):
        for _ in range(10):
            self.service.member_usage.acquire(1, 42, question=True)
        for context in (member_context(member_id=43), member_context(guild_id=2)):
            self.assertEqual((await self.ask(context=context))[0], "Synthetic answer.")

    async def test_concurrent_admission_stops_at_hourly_cap(self):
        async def admit():
            try:
                self.service.member_usage.acquire(1, 42, question=True)
                return "admitted"
            except MemberUsageLimit as error:
                return error.reply
        outcomes = await asyncio.gather(*(admit() for _ in range(20)))
        self.assertEqual(outcomes.count("admitted"), 10)
        self.assertEqual(outcomes.count(""), 9)
        self.assertEqual(outcomes.count("You've hit the hourly limit. Try again in 60 min."), 1)


    async def test_staggered_windows_release_only_expired_requests_and_round_up(self):
        limiter = MemberUsageLimiter()
        limiter.acquire(1, 42, question=True)
        self.clock.return_value += 100
        for _ in range(9):
            limiter.acquire(1, 42, question=True)
        self.clock.return_value = 100_000 + HOUR_SECONDS - 61
        with self.assertRaises(MemberUsageLimit) as refused:
            limiter.acquire(1, 42, question=True)
        self.assertEqual(refused.exception.reply, "You've hit the hourly limit. Try again in 2 min.")
        self.clock.return_value = 100_000 + HOUR_SECONDS
        limiter.acquire(1, 42, question=True)
        with self.assertRaises(MemberUsageLimit) as refused:
            limiter.acquire(1, 42, question=True)
        self.assertEqual(refused.exception.reply, "You've hit the hourly limit. Try again in 2 min.")
        self.assertEqual(len(limiter._members[1, 42].questions), 11)


    async def test_delivery_keeps_repeated_refusal_silent_and_stops_typing(self):
        for _ in range(10):
            self.service.member_usage.acquire(1, 42, question=True)
        await self.ask()
        reply, context = await self.ask()
        stop_typing = AsyncMock()
        context = replace(context, stop_typing=stop_typing)
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        await surface.send_response(context.source_message, reply, None, context=context)
        surface._send_response.assert_not_awaited()
        stop_typing.assert_awaited_once()


    async def test_private_refusals_are_not_cached_or_given_post_controls(self):
        for _ in range(10):
            self.service.member_usage.acquire(1, 42, question=True)
        context = member_context()

        async def private_answer():
            reply, private = await self.ask(context=replace(
                member_context(), private_pass=True,
            ))
            return reply, (), private

        context.state.private_answer_run = private_answer
        view = PrivateAnswerView(context)
        view.post = AsyncMock()
        self.addCleanup(view.stop)
        interaction = SimpleNamespace(
            user=context.member, response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), delete_original_response=AsyncMock(),
        )
        await view.check_privately(interaction)
        self.assertIsNone(view.result)
        interaction.followup.send.assert_awaited_once_with(
            "You've hit the hourly limit. Try again in 60 min.", ephemeral=True,
        )
        await view.check_privately(interaction)
        self.assertIsNone(view.result)
        self.assertEqual(interaction.followup.send.await_count, 1)
        interaction.delete_original_response.assert_awaited_once()
        self.clock.return_value += HOUR_SECONDS
        view.post = None
        await view.check_privately(interaction)
        self.assertEqual(view.result[0], "Synthetic answer.")
        self.assertEqual(interaction.followup.send.await_count, 2)
