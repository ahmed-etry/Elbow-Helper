"""Combined replies preserve independent synthetic action and answer controls."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.answer_view import PrivateAnswerView
from elbow_helper.features.agent.actions.combined_reply import CombinedReplyView
from elbow_helper.features.agent.actions.contracts import ChangePreview, PreparedAction
from elbow_helper.features.agent.actions.preview import ConfirmationView, preview_text
from elbow_helper.features.agent.access import AgentAccessLost, ACCESS_LEAD
from elbow_helper.features.agent.conversation.state import ConversationRecord, ConversationStore, ConversationTurn
from elbow_helper.features.agent.delivery import AgentDeliveryMixin, AgentDeliveryUnknown, _delivery_nonce
from elbow_helper.features.agent.models import (
    AgentAttachment, AgentDelivery, AgentRequestContext, AgentTurnState,
)
from elbow_helper.features.agent.wording import (
    ACTION_CANCELLED, ACTION_PRIVATE_ANSWER, ACTION_PREVIEW_EXPIRED,
    ACTION_CONFIRM_BUTTON, ACTION_CANCEL_BUTTON, ACTION_PRIVATE_BUTTON, ACTION_POST_HERE_BUTTON,
    ACTION_PREVIEW_DETAILS_BUTTON,
)


class CombinedReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.member = SimpleNamespace(id=121, bot=False)
        self.channel = SimpleNamespace(id=221, send=AsyncMock())
        self.guild = SimpleNamespace(id=321, get_member=lambda value: self.member if value == 121 else None)
        self.message = SimpleNamespace(id=421, channel=self.channel, guild=self.guild,
                                       author=self.member, mentions=[], reply=AsyncMock())
        self.sent = []

        async def send(content, **kwargs):
            sent = SimpleNamespace(id=521 + len(self.sent), content=content, view=kwargs.get("view"))

            async def edit(**changes):
                sent.content = changes.get("content", sent.content)
                sent.view = changes.get("view", sent.view)
                return sent

            sent.edit = AsyncMock(side_effect=edit)
            self.sent.append(sent)
            return sent

        self.message.reply.side_effect = send
        self.channel.send.side_effect = send
        self.surface = AgentDeliveryMixin()
        self.surface.bot = SimpleNamespace(user=SimpleNamespace(id=921))
        self.surface._previews = {}
        self.surface._archive_reply = AsyncMock()
        self.surface._conversations = ConversationStore()
        self.surface.action_runner = SimpleNamespace(submit=AsyncMock(return_value="synthetic-run"))
        self.surface.persistence = None
        self.conversation = self.surface._conversations.create(321, 221, 421)
        self.action = PreparedAction(
            "synthetic_change", {"target": 621},
            ChangePreview(("Change synthetic target",), AsyncMock(return_value=True)), AsyncMock(),
        )
        self.context = AgentRequestContext(
            bot=self.surface.bot, guild=self.guild, member=self.member, source_message=self.message,
            account_links=None, message_search=None,
            state=AgentTurnState(source_channels={221}, proposed_changes=[self.action],
                                 preview_reply=preview_text([self.action])),
        )
        self.allowed = AsyncMock(return_value=True)
        for target, mock in (
            ("elbow_helper.features.agent.delivery.can_show", self.allowed),
            ("elbow_helper.features.agent.delivery.require_evidence_access", AsyncMock(return_value=())),
            ("elbow_helper.features.agent.actions.answer_view.require_evidence_access", AsyncMock(return_value=())),
        ):
            patched = patch(target, mock)
            patched.start()
            self.addCleanup(patched.stop)

    def interaction(self):
        return SimpleNamespace(id=721, user=self.member,
                               response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()))

    async def deliver(self, answer="Synthetic lookup answer"):
        delivery = AgentDelivery()
        await self.surface.send_response(
            self.message, answer, None, conversation=self.conversation, context=self.context,
            delivery=delivery,
        )
        for sent in self.sent:
            if sent.view is not None:
                self.addCleanup(sent.view.stop)
        self.conversation.append(ConversationTurn(
            json.dumps({"answer": "\n".join(delivery.text_parts)}), frozenset(), record=ConversationRecord(
                request_message_id=421, member_id=121, created_at="2026-01-01",
                question="Synthetic mixed request", generated_answer=answer,
                delivered_answer="\n".join(delivery.text_parts), local_context="", evidence=(), report_ids=(),
                reply_ids=tuple(delivery.message_ids), delivery_complete=delivery.complete,
                attempted_nonces=tuple(delivery.attempted_nonces), generated_parts=tuple(delivery.generated_parts),
            ),
        ))
        return delivery

    async def test_both_request_orders_have_one_numbered_reply_and_one_delivery(self):
        for first in (True, False):
            with self.subTest(preview_first=first):
                self.context.state.preview_first = first
                delivery = await self.deliver()
                sent = self.sent[-1]
                expected = (self.context.state.preview_reply, "Synthetic lookup answer")
                if not first:
                    expected = expected[::-1]
                self.assertEqual(sent.content, f"1. {expected[0]}\n\n2. {expected[1]}")
                self.assertEqual(len(delivery.message_ids), 1)
                self.assertEqual(delivery.attempted_nonces, [_delivery_nonce(421, 0)])
                self.assertTrue(delivery.complete)
                self.assertIs(self.surface._conversations.find(321, 221, sent.id), self.conversation)
                self.surface._archive_reply.assert_any_await(421, sent.id, sent.content)

    async def test_published_workbook_keep_links_when_the_preview_changes(self):
        attachments = [AgentAttachment(
            f"synthetic-{index}.xlsx", b"Synthetic workbook",
            google_link=f"https://docs.google.com/spreadsheets/d/synthetic-{index}/edit",
            spreadsheet_title=f"Synthetic export {index}",
        ) for index in range(1)]
        await self.surface.send_response(
            self.message, "Synthetic workbooks ready", None, attachments, context=self.context,
        )
        combined = self.sent[0].view
        self.addCleanup(combined.stop)
        self.assertNotIn("files", self.message.reply.await_args.kwargs)
        links = [item for item in combined.children if getattr(item, "url", None)]
        self.assertEqual(len(links), 2)
        self.assertEqual([item.label for item in links], [
            "Google Sheet", "Download",
        ])
        self.assertEqual([item.row for item in links], [1, 1])
        await combined.views["preview"].cancel(self.interaction())
        remaining = [item for item in self.sent[0].view.children if getattr(item, "url", None)]
        self.assertEqual(remaining, links)
        self.assertTrue(all(not item.disabled for item in remaining))

    async def test_cancel_and_expiry_replace_only_the_preview_in_either_order(self):
        for first in (True, False):
            for cancel in (True, False):
                with self.subTest(preview_first=first, cancel=cancel):
                    self.context.state.preview_first = first
                    await self.deliver()
                    combined = self.sent[-1].view
                    preview = combined.views["preview"]
                    if cancel:
                        await preview.cancel(self.interaction())
                    else:
                        await preview.on_timeout()
                    expected = ACTION_CANCELLED if cancel else ACTION_PREVIEW_EXPIRED
                    self.assertEqual(combined.parts["preview"], expected)
                    self.assertEqual(combined.parts["answer"], "Synthetic lookup answer")
                    self.assertEqual(self.sent[-1].content, combined.render())
                    self.assertIn("Synthetic lookup answer", self.conversation.turns[-1].record.delivered_answer)

    async def test_confirm_hands_off_only_the_preview_part(self):
        await self.deliver()
        combined = self.sent[0].view
        preview = combined.views["preview"]
        await preview.confirm(self.interaction())
        await asyncio.sleep(0)
        self.assertFalse(combined.is_finished())
        progress = self.surface.action_runner.submit.await_args.kwargs["progress_message"]
        self.assertTrue(progress.preserves_other_text)
        await progress.edit(content="Synthetic run report", view=None)
        self.assertEqual(self.sent[0].content, "1. Synthetic lookup answer\n\n2. Synthetic run report")
        self.assertEqual(self.conversation.turns[0].record.reply_ids, (521,))
        self.assertIn("Synthetic run report", self.conversation.turns[0].record.delivered_answer)

    async def test_private_answer_has_four_buttons_and_posts_only_its_part_in_place(self):
        self.allowed.return_value = False
        self.context.state.preview_first = True
        await self.deliver()
        combined = self.sent[0].view
        self.assertEqual([item.label for item in combined.children], [
            ACTION_CONFIRM_BUTTON, ACTION_CANCEL_BUTTON, ACTION_PRIVATE_BUTTON, ACTION_POST_HERE_BUTTON,
        ])
        self.assertEqual(combined.parts["answer"], ACTION_PRIVATE_ANSWER)
        private = combined.views["answer"]
        await private.open_result(self.interaction())
        preview = combined.parts["preview"]
        await private.post_here(self.interaction())
        self.assertEqual(self.message.reply.await_count, 1)
        self.assertEqual(combined.parts, {"preview": preview, "answer": "Synthetic lookup answer"})
        self.assertEqual([item.label for item in combined.children], [ACTION_CONFIRM_BUTTON, ACTION_CANCEL_BUTTON])
        self.assertTrue(all(not item.disabled for item in combined.children))
        self.assertEqual(self.conversation.turns[0].record.reply_ids, (521,))
        self.assertEqual(self.conversation.turns[0].record.delivered_answer, combined.render())
        self.surface._archive_reply.assert_any_await(
            421, 521, combined.render(), previous_content=f"1. {preview}\n\n2. {ACTION_PRIVATE_ANSWER}",
        )

    async def test_hidden_details_keep_their_control_beside_both_button_sets(self):
        self.allowed.return_value = False
        action = replace(self.action, preview=replace(
            self.action.preview, details=("Synthetic hidden detail",), detail_access=frozenset({ACCESS_LEAD}),
        ))
        self.context.state.proposed_changes[:] = [action]
        # Delivery must replace wording prepared while a broader audience was allowed.
        self.context.state.preview_reply = preview_text([action])
        with patch("elbow_helper.features.agent.actions.details.can_disclose_provenance",
                   AsyncMock(return_value=False)):
            await self.deliver()
        combined = self.sent[0].view
        self.assertIn(ACTION_PREVIEW_DETAILS_BUTTON, [item.label for item in combined.children])
        self.assertEqual(len(combined.children), 5)
        self.assertNotIn("Synthetic hidden detail", combined.render())
        self.assertNotIn("Synthetic hidden detail", self.context.state.preview_reply)
        with (patch("elbow_helper.features.agent.actions.preview.require_access"),
              patch("elbow_helper.features.agent.actions.preview.require_access_requirements")):
            interaction = self.interaction()
            await combined.views["preview"].show_details(interaction)
        self.assertEqual(interaction.response.send_message.await_args.args[0], "Synthetic hidden detail")
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_revoked_private_answer_access_expires_only_its_controls(self):
        self.allowed.return_value = False
        await self.deliver()
        combined = self.sent[0].view
        with patch("elbow_helper.features.agent.actions.answer_view.require_evidence_access",
                   AsyncMock(side_effect=AgentAccessLost("Synthetic access loss"))):
            await combined.views["answer"].post_here(self.interaction())
        self.assertEqual(self.message.reply.await_count, 1)
        self.assertEqual(combined.parts["answer"], ACTION_PRIVATE_ANSWER)
        self.assertTrue(all(child.disabled for child in combined.views["answer"].children))
        self.assertTrue(all(not child.disabled for child in combined.views["preview"].children))

    async def test_private_ping_fallback_keeps_request_order_and_distinct_nonces(self):
        self.allowed.return_value = False
        for first in (True, False):
            with self.subTest(preview_first=first):
                self.context.state.preview_first = first
                before = self.message.reply.await_count
                await self.deliver("Synthetic answer <@121>")
                calls = self.message.reply.await_args_list[before:]
                expected = [self.context.state.preview_reply, ACTION_PRIVATE_ANSWER]
                if not first:
                    expected.reverse()
                self.assertEqual([call.args[0] for call in calls], expected)
                self.assertNotEqual(calls[0].kwargs["nonce"], calls[1].kwargs["nonce"])

    async def test_message_limit_includes_numbering_and_separator(self):
        self.context.state.preview_first = True
        overhead = len(f"1. {self.context.state.preview_reply}\n\n2. ")
        await self.deliver("x" * (2000 - overhead))
        self.assertEqual(len(self.sent[0].content), 2000)
        self.assertIsInstance(self.sent[0].view, CombinedReplyView)
        before = self.message.reply.await_count
        await self.deliver("x" * (2001 - overhead))
        self.assertEqual(self.message.reply.await_count - before, 2)

    async def test_post_here_that_does_not_fit_posts_separately_and_keeps_confirmation(self):
        self.allowed.return_value = False
        await self.deliver("Synthetic answer " + "x" * 1950)
        combined = self.sent[0].view
        self.assertIsInstance(combined, CombinedReplyView)
        await combined.views["answer"].post_here(self.interaction())
        self.assertEqual(self.message.reply.await_count, 2)
        self.assertTrue(all(not child.disabled for child in combined.views["preview"].children))
        self.assertEqual(self.conversation.turns[0].record.reply_ids, (521, 522))
        self.assertIn("x" * 1950, json.loads(self.conversation.turns[0].text)["answer"])
        self.assertEqual(self.conversation.turns[0].record.attempted_nonces,
                         (_delivery_nonce(421, 0), _delivery_nonce(721, 0)))

    async def test_private_and_preview_controls_expire_independently(self):
        for preview_first in (True, False):
            with self.subTest(preview_expires_first=preview_first):
                preview = ConfirmationView(121, (self.action,), self.context,
                                           timeout=0.01 if preview_first else 0.1)
                private = PrivateAnswerView(self.context, "Synthetic answer", (), AsyncMock(),
                                            timeout=0.1 if preview_first else 0.01)
                combined = CombinedReplyView(self.context.state.preview_reply, ACTION_PRIVATE_ANSWER,
                                             preview, private, preview_first=True, on_change=AsyncMock())
                self.addCleanup(combined.stop)
                sent = await self.message.reply(combined.render())
                combined.start(sent)
                first, last = (preview, private) if preview_first else (private, preview)
                await asyncio.wait_for(first.wait(), timeout=1)
                self.assertTrue(all(child.disabled for child in first.children))
                self.assertTrue(all(not child.disabled for child in last.children))
                if not preview_first:
                    self.assertEqual(combined.parts["preview"], self.context.state.preview_reply)
                self.assertEqual(combined.parts["answer"], ACTION_PRIVATE_ANSWER)
                await asyncio.wait_for(last.wait(), timeout=1)

    async def test_over_limit_and_ping_answers_fall_back_in_request_order(self):
        for first in (True, False):
            for ping in (True, False):
                with self.subTest(preview_first=first, ping=ping):
                    self.context.state.preview_first = first
                    response = "Synthetic answer <@121>" if ping else "Synthetic " + "x" * 1940
                    before = self.message.reply.await_count
                    await self.deliver(response)
                    calls = self.message.reply.await_args_list[before:]
                    self.assertEqual(len(calls), 2)
                    expected = [self.context.state.preview_reply, response]
                    if not first:
                        expected.reverse()
                    self.assertEqual([call.args[0] for call in calls], expected)
                    preview_call, answer_call = calls if first else calls[::-1]
                    self.assertFalse(preview_call.kwargs["allowed_mentions"].users)
                    if ping:
                        self.assertIn(self.member, answer_call.kwargs["allowed_mentions"].users)
                    self.assertNotEqual(calls[0].kwargs["nonce"], calls[1].kwargs["nonce"])

    async def test_changes_only_delivery_is_unchanged(self):
        self.context.state.preview_reply = None
        await self.surface.send_response(
            self.message, preview_text([self.action]), None, context=self.context,
        )
        self.assertEqual(self.message.reply.await_count, 1)
        self.assertIsInstance(self.sent[0].view, ConfirmationView)
        self.assertEqual(self.sent[0].content, preview_text([self.action]))
        self.allowed.assert_not_awaited()

    async def test_uncertain_combined_send_retains_one_nonce_and_complete_generated_part(self):
        self.message.reply.side_effect = OSError("Synthetic uncertain delivery")
        self.surface._find_delivery_nonce = AsyncMock(return_value=None)
        delivery = AgentDelivery()
        with self.assertRaises(AgentDeliveryUnknown):
            await self.surface.send_response(
                self.message, "Synthetic answer", None, context=self.context, delivery=delivery,
            )
        self.assertTrue(delivery.unknown)
        self.assertFalse(delivery.complete)
        self.assertEqual(delivery.attempted_nonces, [_delivery_nonce(421, 0)])
        self.assertEqual(delivery.generated_parts,
                         [f"1. Synthetic answer\n\n2. {self.context.state.preview_reply}"])

    async def test_concurrent_answer_post_and_progress_preserve_both_edits(self):
        self.allowed.return_value = False
        await self.deliver()
        combined = self.sent[0].view
        private = combined.views["answer"]
        await asyncio.gather(combined.views["preview"].message.edit(content="Synthetic progress"),
                             private.post_here(self.interaction()))
        self.assertEqual(combined.parts, {"preview": "Synthetic progress", "answer": "Synthetic lookup answer"})
        self.assertEqual(self.sent[0].content, combined.render())
