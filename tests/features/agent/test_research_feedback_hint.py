"""Occasional feedback hints at the final Discord delivery boundary."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.actions.contracts import ChangePreview, PreparedAction
from elbow_helper.features.agent.actions.preview import preview_text
from elbow_helper.features.agent.delivery import (
    AgentDeliveryMixin, MAX_RESPONSE_CHARACTERS, RESEARCH_FEEDBACK_HINT_INTERVAL,
)
from elbow_helper.features.agent.engine.service import AgentService, AgentUnavailableError
from elbow_helper.features.agent.models import AgentAttachment, AgentDelivery
from elbow_helper.features.agent.text import DISCORD_MESSAGE_LIMIT, chunk_response
from elbow_helper.features.agent.wording import (
    ACTION_NOT_PREPARED, ACTION_PRIVATE_ANSWER, ACTION_PRIVATE_NOTE, ACTION_UNAVAILABLE,
    AGENT_ANSWER_UNFINISHED, AGENT_PLAN_UNFINISHED, AGENT_RESEARCH_UNFINISHED,
    FAILURE_MESSAGE, RESEARCH_FEEDBACK_HINT,
)
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage, TextGenerationError
from features.agent.engine.helpers import social_step
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession


class ResearchFeedbackHintTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
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
        self.sender._research_feedback_answers = RESEARCH_FEEDBACK_HINT_INTERVAL - 1

    async def test_every_fifth_completed_research_answer_gets_exact_final_line(self):
        self.assertEqual(RESEARCH_FEEDBACK_HINT, "-# React \U0001f44e if this answer was off")
        for number in range(1, 11):
            answer = await self.answer()
            self.assertTrue(self.context.state.research_answer_completed)
            self.assertNotIn(RESEARCH_FEEDBACK_HINT, answer)
            parts, _ = await self.deliver(answer)
            expected = "Synthetic finding."
            if number % RESEARCH_FEEDBACK_HINT_INTERVAL == 0:
                expected += "\n" + RESEARCH_FEEDBACK_HINT
            self.assertEqual(parts, [expected])

    async def test_social_answer_never_gets_hint_or_advances_counter(self):
        self.next_hint_due()
        answer = await self.answer(route="social", steps=[social_step("Synthetic banter.")])
        self.assertFalse(self.context.state.research_answer_completed)
        self.assertEqual((await self.deliver(answer))[0], ["Synthetic banter."])
        answer = await self.answer()
        self.assertEqual((await self.deliver(answer))[0][-1].splitlines()[-1], RESEARCH_FEEDBACK_HINT)

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

    async def test_fixed_errors_and_private_notices_never_get_hint(self):
        for text in (FAILURE_MESSAGE, AGENT_ANSWER_UNFINISHED, AGENT_PLAN_UNFINISHED,
                     AGENT_RESEARCH_UNFINISHED, ACTION_NOT_PREPARED, ACTION_UNAVAILABLE,
                     ACTION_PRIVATE_NOTE, ACTION_PRIVATE_ANSWER):
            with self.subTest(text=text):
                self.next_hint_due()
                parts, _ = await self.deliver(text)
                self.assertNotIn(RESEARCH_FEEDBACK_HINT, "\n".join(parts))
                self.assertEqual(self.sender._research_feedback_answers, RESEARCH_FEEDBACK_HINT_INTERVAL - 1)

    def test_previews_private_results_and_followup_repairs_are_ineligible(self):
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
                self.assertFalse(self.sender._research_feedback_hint_due(context, "Synthetic answer", ["Synthetic answer"]))
        for changes in ({"private_pass": True}, {"dm_delivery": True}, {"history": (object(),)}):
            with self.subTest(changes=changes):
                self.assertFalse(self.sender._research_feedback_hint_due(
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
                    self.assertEqual(self.sender._research_feedback_answers, RESEARCH_FEEDBACK_HINT_INTERVAL - 1)
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

    async def test_attachment_only_reply_has_no_hint(self):
        for warning in (None, "Synthetic publication warning."):
            with self.subTest(warning=warning):
                self.next_hint_due()
                parts, _ = await self.deliver("", attachments=(
                    AgentAttachment("synthetic.txt", b"synthetic", google_warning=warning),
                ))
                self.assertEqual(parts, [warning])
                self.assertEqual(self.sender._research_feedback_answers, RESEARCH_FEEDBACK_HINT_INTERVAL - 1)

    async def test_gif_only_reply_never_gets_hint_or_advances_counter(self):
        self.next_hint_due()
        self.context.state.gif_url = "https://tenor.com/view/synthetic-123"
        parts, _ = await self.deliver(self.context.state.gif_url)
        self.assertEqual(parts, [self.context.state.gif_url])
        self.assertEqual(self.sender._research_feedback_answers, RESEARCH_FEEDBACK_HINT_INTERVAL - 1)

    async def test_file_fallback_keeps_original_answer_and_has_no_hint_only_message(self):
        self.next_hint_due()
        text = "x" * (MAX_RESPONSE_CHARACTERS + 1)
        parts, delivery = await self.deliver(text)
        self.assertEqual(parts, [None])
        self.assertEqual(delivery.text_parts, [text])
        file, = self.context.source_message.reply.call_args.kwargs["files"]
        self.assertEqual(file.fp.getvalue().decode("utf-8"), text)
