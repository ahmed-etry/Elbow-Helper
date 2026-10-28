"""Member allowances through routing, escalation and Discord delivery."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import (
    AGENT_TESTER_ROLE_ID, CORE, LEAD_PLUS, MEMBER_ROLE_ID,
    SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID,
)
from elbow_helper.agent.delivery import AgentDeliveryMixin
from elbow_helper.agent.actions.answer_view import PrivateAnswerView
from elbow_helper.agent.engine.member_usage import (
    DAY_SECONDS, HOUR_SECONDS, MemberUsageLimit, MemberUsageLimiter,
)
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.knowledge.store import KnowledgeCatalog
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from agent.engine.helpers import social_step
from agent.engine.test_agent_service import _AgentModel, _AgentSession, _context


def member_context(role=MEMBER_ROLE_ID, *, guild_id=1, member_id=42):
    context = _context()
    context.member.id = member_id
    context.guild.id = guild_id
    context.member.roles = [SimpleNamespace(id=identity) for identity in (AGENT_TESTER_ROLE_ID, role)]
    context.guild.roles = [context.guild.default_role, *context.member.roles]
    return context


class MemberUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = self.enterContext(patch(
            "elbow_helper.agent.engine.member_usage.monotonic", return_value=100_000.0,
        ))
        self.enterContext(patch(
            "elbow_helper.agent.engine.service.build_agent_tools", return_value={},
        ))
        self.directory = self.enterContext(patch(
            "elbow_helper.agent.engine.service.scoped_channel_directory",
            AsyncMock(return_value="[]"),
        ))
        self.session = _AgentSession([])
        self.model = _AgentModel(self.session)
        self.service = AgentService(self.model)

    async def ask(self, route="social", *, context=None, steps=None):
        self.model.route = route
        self.model.request = None
        self.session.steps = list(steps) if steps is not None else [
            social_step("Synthetic banter.") if route == "social"
            else AgentStep("Synthetic answer.", (), AgentUsage()),
        ]
        context = context or member_context()
        reply = await self.service.answer(question="Synthetic request", local_context="", context=context)
        return reply, context

    async def test_thirty_banter_replies_then_one_notice_and_silence_across_channels(self):
        for _ in range(30):
            reply, _ = await self.ask()
            self.assertEqual(reply, "Synthetic banter.")
        self.directory.assert_not_awaited()
        for expected in ("You've hit the hourly limit. Try again in 60 min.", "", ""):
            context = member_context()
            context.source_message.channel.id = 200
            reply, context = await self.ask(context=context)
            self.assertEqual(reply, expected)
            self.assertTrue(context.state.member_limit_refused)
            self.assertIsNone(self.model.request)
            self.assertEqual(len(self.model.routing_session.calls), 1)
        self.assertEqual(len(self.session.calls), 30)
        self.assertEqual(len(self.service._member_usage._members[1, 42].banter), 30)

    async def test_ten_questions_then_notice_without_spending_banter_allowance(self):
        for _ in range(10):
            self.assertEqual((await self.ask("research"))[0], "Synthetic answer.")
        self.directory.reset_mock()
        for expected in ("You've hit the hourly limit. Try again in 60 min.", ""):
            self.assertEqual((await self.ask("research"))[0], expected)
            self.assertIsNone(self.model.request)
        self.directory.assert_not_awaited()
        self.assertEqual(len(self.session.calls), 10)
        self.assertEqual((await self.ask())[0], "Synthetic banter.")
        self.assertEqual(len(self.service._member_usage._members[1, 42].questions), 10)

    async def test_forty_daily_questions_across_hours_and_daily_rollover(self):
        for hour in range(4):
            self.clock.return_value = 100_000 + hour * HOUR_SECONDS
            for _ in range(10):
                self.assertEqual((await self.ask("research"))[0], "Synthetic answer.")
        self.clock.return_value += HOUR_SECONDS
        self.assertEqual((await self.ask("research"))[0],
                         "You've hit the daily limit. Try again in 20 h.")
        self.assertEqual((await self.ask("research"))[0], "")
        self.assertIsNone(self.model.request)
        self.clock.return_value = 100_000 + DAY_SECONDS
        self.assertEqual((await self.ask("research"))[0], "Synthetic answer.")

    async def test_leadership_including_sleeping_roles_bypasses_both_caps(self):
        for role in sorted(LEAD_PLUS | CORE | {SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID}):
            with self.subTest(role=role):
                # Even existing member usage must not cap a newly promoted leader.
                for _ in range(30):
                    try:
                        self.service._member_usage.acquire(1, 42, question=False)
                    except MemberUsageLimit:
                        pass
                for _ in range(10):
                    try:
                        self.service._member_usage.acquire(1, 42, question=True)
                    except MemberUsageLimit:
                        pass
                with patch.object(self.service._member_usage, "acquire",
                                  side_effect=AssertionError("Leadership was counted")):
                    for route in ("social", "game_advice", "research"):
                        reply, context = await self.ask(route, context=member_context(role))
                        self.assertTrue(reply)
                        self.assertFalse(context.state.member_limit_refused)

    async def test_game_advice_brush_off_uses_banter_allowance(self):
        for _ in range(30):
            self.assertEqual((await self.ask("game_advice"))[0], "Synthetic answer.")
        self.assertEqual((await self.ask("game_advice"))[0],
                         "You've hit the hourly limit. Try again in 60 min.")
        self.assertIsNone(self.model.request)
        self.assertEqual((await self.ask("research"))[0], "Synthetic answer.")

    async def test_game_advice_with_knowledge_is_a_question(self):
        context = replace(member_context(), knowledge_store=SimpleNamespace(
            load=lambda: KnowledgeCatalog("synthetic", ()),
        ))
        self.assertEqual((await self.ask("game_advice", context=context))[0], "Synthetic answer.")
        usage = self.service._member_usage._members[1, 42]
        self.assertEqual((len(usage.banter), len(usage.questions)), (0, 1))

    async def test_escalation_reclassifies_banter_as_a_question(self):
        steps = [AgentStep("", (AgentToolCall("expand", "load_research_context", "{}"),), AgentUsage()),
                 AgentStep("Synthetic researched answer.", (), AgentUsage())]
        self.assertEqual((await self.ask(steps=steps))[0], "Synthetic researched answer.")
        usage = self.service._member_usage._members[1, 42]
        self.assertEqual((len(usage.banter), len(usage.questions)), (0, 1))
        self.assertEqual(self.session.efforts, [AgentReasoningEffort.LOW, AgentReasoningEffort.HIGH])

    async def test_capped_escalation_preserves_banter_without_research_or_notice(self):
        for _ in range(10):
            await self.ask("research")
        self.directory.reset_mock()
        self.session.calls.clear()
        steps = [AgentStep("Synthetic banter stands.", (
            AgentToolCall("expand", "load_research_context", "{}"),), AgentUsage())]
        self.assertEqual((await self.ask(steps=steps))[0], "Synthetic banter stands.")
        self.directory.assert_not_awaited()
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.session.tool_replacements, [])
        usage = self.service._member_usage._members[1, 42]
        self.assertEqual((len(usage.banter), len(usage.questions)), (1, 10))
        self.assertEqual(usage.notices, {})
        self.assertEqual((await self.ask("research"))[0],
                         "You've hit the hourly limit. Try again in 60 min.")

    async def test_capped_empty_escalation_finishes_only_banter_at_low_effort(self):
        for _ in range(10):
            await self.ask("research")
        self.session.calls.clear()
        self.session.efforts.clear()
        self.directory.reset_mock()
        steps = [AgentStep("", (AgentToolCall("expand", "load_research_context", "{}"),), AgentUsage()),
                 AgentStep("Synthetic banter.", (), AgentUsage())]
        self.assertEqual((await self.ask(steps=steps))[0], "Synthetic banter.")
        self.directory.assert_not_awaited()
        self.assertEqual(self.session.efforts, [AgentReasoningEffort.LOW] * 2)
        self.assertEqual(self.session.tool_replacements, [()])
        self.assertFalse(self.session.calls[-1][1])

    async def test_rolling_hour_frees_a_slot_and_resets_notice(self):
        for _ in range(10):
            await self.ask("research")
        self.clock.return_value += HOUR_SECONDS - 1
        self.assertEqual((await self.ask("research"))[0],
                         "You've hit the hourly limit. Try again in 1 min.")
        self.clock.return_value += 1
        for _ in range(10):
            self.assertEqual((await self.ask("research"))[0], "Synthetic answer.")
        self.assertEqual((await self.ask("research"))[0],
                         "You've hit the hourly limit. Try again in 60 min.")

    async def test_guilds_and_members_have_independent_allowances(self):
        for _ in range(10):
            await self.ask("research")
        for context in (member_context(member_id=43), member_context(guild_id=2)):
            self.assertEqual((await self.ask("research", context=context))[0], "Synthetic answer.")

    async def test_concurrent_admission_stops_at_hourly_cap(self):
        async def admit():
            try:
                self.service._member_usage.acquire(1, 42, question=True)
                return "admitted"
            except MemberUsageLimit as error:
                return error.reply
        outcomes = await asyncio.gather(*(admit() for _ in range(20)))
        self.assertEqual(outcomes.count("admitted"), 10)
        self.assertEqual(outcomes.count(""), 9)
        self.assertEqual(outcomes.count("You've hit the hourly limit. Try again in 60 min."), 1)

    async def test_old_members_are_pruned_and_restart_resets_usage(self):
        for _ in range(10):
            self.service._member_usage.acquire(1, 42, question=True)
        self.assertIsNotNone(MemberUsageLimiter().acquire(1, 42, question=True))
        self.clock.return_value += DAY_SECONDS
        self.service._member_usage.acquire(2, 43, question=False)
        self.assertNotIn((1, 42), self.service._member_usage._members)

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

    async def test_daily_notice_rounds_up_and_a_capped_escalation_keeps_banter(self):
        for hour in range(4):
            self.clock.return_value = 100_000 + hour * HOUR_SECONDS
            for _ in range(10):
                self.service._member_usage.acquire(1, 42, question=True)
        self.clock.return_value = 100_000 + DAY_SECONDS - 1
        steps = [AgentStep("Synthetic banter stands.", (
            AgentToolCall("expand", "load_research_context", "{}"),), AgentUsage())]
        self.assertEqual((await self.ask(steps=steps))[0], "Synthetic banter stands.")
        self.assertEqual((await self.ask("research"))[0],
                         "You've hit the daily limit. Try again in 1 h.")
        self.assertEqual((await self.ask("research"))[0], "")

    async def test_hourly_notice_is_shared_between_banter_and_questions(self):
        for question, count in ((False, 30), (True, 10)):
            for _ in range(count):
                self.service._member_usage.acquire(1, 42, question=question)
        self.assertEqual((await self.ask())[0],
                         "You've hit the hourly limit. Try again in 60 min.")
        self.assertEqual((await self.ask("research"))[0], "")

    async def test_delivery_keeps_repeated_refusal_silent_and_stops_typing(self):
        for _ in range(10):
            await self.ask("research")
        await self.ask("research")
        reply, context = await self.ask("research")
        stop_typing = AsyncMock()
        context = replace(context, stop_typing=stop_typing)
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        await surface.send_response(context.source_message, reply, None, context=context)
        surface._send_response.assert_not_awaited()
        stop_typing.assert_awaited_once()

    async def test_limit_notice_is_delivered_without_private_offer(self):
        for _ in range(10):
            await self.ask("research")
        context = member_context()
        context.state.scope_audit.offered = True
        reply, context = await self.ask("research", context=context)
        self.assertFalse(context.state.scope_audit.offered)
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        await surface.send_response(context.source_message, reply, None, context=context)
        self.assertEqual(surface._send_response.await_args.args[1],
                         "You've hit the hourly limit. Try again in 60 min.")

    async def test_private_refusals_are_not_cached_or_given_post_controls(self):
        for _ in range(10):
            await self.ask("research")
        context = member_context()

        async def private_answer():
            reply, private = await self.ask("research", context=replace(
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
