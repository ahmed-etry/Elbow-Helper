"""Answers and confirmation cards use independent messages and controls."""

import json
from dataclasses import replace
from types import SimpleNamespace
import unittest

import discord
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.actions.contracts import ChangePreview, PreparedAction
from elbow_helper.agent.actions.preview import preview_text
from elbow_helper.agent.safety.access import ACCESS_LEAD
from elbow_helper.agent.conversation.state import ConversationRecord, ConversationStore, ConversationTurn
from elbow_helper.agent.conversation.delivery import AgentDeliveryMixin, AgentDeliveryUnknown, _delivery_nonce
from elbow_helper.agent.engine.models import AgentAttachment, AgentDelivery, AgentRequestContext, AgentTurnState
from elbow_helper.agent.conversation.wording import ACTION_CANCELLED, ACTION_PREVIEW_EXPIRED, ACTION_PREVIEW_REPLACED, ACTION_PREVIEW_DETAILS_BUTTON


class ConfirmationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.member = SimpleNamespace(id=121, bot=False, display_name="Requester")
        self.channel = SimpleNamespace(id=221, send=AsyncMock())
        self.guild = SimpleNamespace(id=321, get_member=lambda value: self.member if value == 121 else None)
        self.message = SimpleNamespace(id=421, channel=self.channel, guild=self.guild,
                                       author=self.member, mentions=[], reply=AsyncMock())
        self.sent = []

        async def send(content, **kwargs):
            sent = SimpleNamespace(id=521 + len(self.sent), content=content, view=kwargs.get("view"),
                                   embed=kwargs.get("embed"))

            async def edit(**changes):
                sent.content = changes.get("content", sent.content)
                sent.view = changes.get("view", sent.view)
                sent.embed = changes.get("embed", sent.embed)
                return sent

            sent.edit = AsyncMock(side_effect=edit)
            self.sent.append(sent)
            return sent

        self.message.reply.side_effect = send
        self.channel.send.side_effect = send
        self.surface = AgentDeliveryMixin()
        self.surface.bot = SimpleNamespace(user=SimpleNamespace(id=921))
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
            ("elbow_helper.agent.conversation.delivery.assert_answer_scope", self.allowed),
            ("elbow_helper.agent.conversation.delivery.require_evidence_access", AsyncMock(return_value=())),
            ("elbow_helper.agent.actions.answer_view.require_evidence_access", AsyncMock(return_value=())),
        ):
            patched = patch(target, mock)
            patched.start()
            self.addCleanup(patched.stop)

    def interaction(self, message=None):
        return SimpleNamespace(id=721, user=self.member,
                               response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(),
                                                        edit_message=message.edit if message else AsyncMock()),
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


    async def test_a_direct_reply_retires_the_members_earlier_open_card(self):
        for request_id, name in ((421, "CWL talk"), (422, "CWL rosters")):
            reference = (SimpleNamespace(channel_id=self.channel.id, message_id=self.sent[-1].id)
                         if self.sent else None)
            message = SimpleNamespace(**(vars(self.message) | {"id": request_id, "reference": reference}))
            action = replace(
                self.action, path="create_discord_thread",
                values={"parent_channel_id": self.channel.id, "name": name},
                preview=replace(self.action.preview, lines=(f"Create thread {name}.",)),
            )
            context = replace(self.context, source_message=message, state=AgentTurnState(
                source_channels={self.channel.id}, proposed_changes=[action],
                preview_reply=preview_text([action]),
            ))
            await self.surface.send_response(
                message, f"Ready to create {name}.", None,
                conversation=self.conversation, context=context,
            )
            self.addCleanup(self.sent[-1].view.stop)

        earlier, revised = self.sent[1].view, self.sent[3].view
        click = self.interaction()
        await earlier.confirm(click)
        click.response.send_message.assert_awaited_once_with(ACTION_PREVIEW_REPLACED, ephemeral=True)
        self.surface.action_runner.submit.assert_not_awaited()
        await revised.confirm(self.interaction())
        self.surface.action_runner.submit.assert_awaited_once()
        self.assertEqual(self.surface.action_runner.submit.await_args.args[1][0].values["name"],
                         "CWL rosters")


    async def test_image_answer_is_separate_from_confirmation_and_has_no_caption(self):
        delivery = AgentDelivery()
        self.context.state.preview_first = True
        await self.surface.send_response(self.message, 'Synthetic fallback.', None,
            [AgentAttachment('sample.png', b'media')], context=self.context, delivery=delivery)
        answer = self.message.reply.await_args
        card = self.channel.send.await_args
        self.assertIsNone(answer.args[0])
        self.assertEqual(len(answer.kwargs["files"]), 1)
        self.assertIsNone(card.args[0])
        self.assertNotIn("files", card.kwargs)
        self.assertEqual(card.kwargs["embed"].title, "Confirm 1 change")
        self.assertTrue(delivery.complete)
        for sent in self.sent:
            if sent.view is not None:
                sent.view.stop()

    async def test_published_workbook_keep_links_when_the_preview_changes(self):
        attachments = [AgentAttachment(
            f"synthetic-{index}.xlsx", b"Synthetic workbook",
            google_link=f"https://docs.google.com/spreadsheets/d/synthetic-{index}/edit",
            spreadsheet_title=f"Synthetic export {index}",
        ) for index in range(1)]
        await self.surface.send_response(
            self.message, "Synthetic workbooks ready", None, attachments, context=self.context,
        )
        answer, card = self.sent
        self.addCleanup(answer.view.stop)
        self.addCleanup(card.view.stop)
        self.assertNotIn("files", self.message.reply.await_args.kwargs)
        links = [item for item in answer.view.children if getattr(item, "url", None)]
        self.assertEqual(len(links), 2)
        self.assertEqual([item.label for item in links], [
            "Google Sheet", "Download",
        ])
        self.assertEqual([item.row for item in links], [0, 0])
        await card.view.cancel(self.interaction(card))
        remaining = [item for item in self.sent[0].view.children if getattr(item, "url", None)]
        self.assertEqual(remaining, links)
        self.assertTrue(all(not item.disabled for item in remaining))

    async def test_cancel_and_expiry_edit_only_the_card(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                await self.deliver()
                answer, card = self.sent[-2:]
                if cancel:
                    await card.view.cancel(self.interaction(card))
                else:
                    await card.view.on_timeout()
                expected = ACTION_CANCELLED if cancel else ACTION_PREVIEW_EXPIRED
                self.assertEqual(card.embed.description, expected)
                self.assertEqual(card.embed.colour, discord.Colour.greyple())
                self.assertIsNone(card.view)
                self.assertEqual(answer.content, "Synthetic lookup answer")
                answer.edit.assert_not_awaited()

    async def test_two_change_card_quotes_dm_with_real_blank_lines(self):
        dm = replace(self.action, path="send_direct_messages", preview=ChangePreview(
            ("DM Requester",), AsyncMock(return_value=True), summary="Send DMs",
            details=("First line", "", "Second line"), message_body=slice(None),
        ))
        self.context.state.proposed_changes.append(dm)
        await self.deliver()
        answer, card = self.sent
        self.assertEqual(answer.content, "Synthetic lookup answer")
        self.assertIsNone(card.content)
        self.assertEqual(card.embed.title, "Confirm 2 changes")
        self.assertEqual(card.embed.footer.text, "Only Requester can confirm \u00b7 expires in 5 min")
        self.assertEqual(card.embed.colour, discord.Colour.blue())
        self.assertIn("> First line\n> \n> Second line", card.embed.description)
        self.assertNotIn("(blank line)", card.embed.description)
        self.assertEqual([button.label for button in card.view.children], ["Confirm", "Cancel"])

    async def test_hidden_details_stay_off_the_public_card(self):
        self.allowed.return_value = False
        action = replace(self.action, preview=replace(
            self.action.preview, details=("Synthetic hidden detail",), detail_access=frozenset({ACCESS_LEAD}),
        ))
        self.context.state.proposed_changes[:] = [action]
        # Delivery must replace wording prepared while a broader audience was allowed.
        self.context.state.preview_reply = preview_text([action])
        with patch("elbow_helper.agent.actions.details.can_disclose_provenance",
                   AsyncMock(return_value=False)):
            await self.deliver()
        card = self.sent[-1]
        self.assertIn(ACTION_PREVIEW_DETAILS_BUTTON, [item.label for item in card.view.children])
        self.assertEqual(len(card.view.children), 3)
        self.assertNotIn("Synthetic hidden detail", card.embed.description)
        self.assertNotIn("Synthetic hidden detail", self.context.state.preview_reply)
        with (patch("elbow_helper.agent.actions.preview.require_access"),
              patch("elbow_helper.agent.actions.preview.require_access_requirements")):
            interaction = self.interaction()
            await card.view.show_details(interaction)
        self.assertEqual(interaction.response.send_message.await_args.args[0], "Synthetic hidden detail")
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])


    async def test_long_or_ping_answer_still_precedes_the_card(self):
        for ping in (True, False):
            with self.subTest(ping=ping):
                response = "Synthetic answer <@121>" if ping else "Synthetic " + "x" * 1940
                await self.deliver(response)
                answer, card = self.message.reply.await_args, self.channel.send.await_args
                self.assertEqual(answer.args[0], response)
                self.assertIsNone(card.args[0])
                self.assertFalse(card.kwargs["allowed_mentions"].users)
                if ping:
                    self.assertIn(self.member, answer.kwargs["allowed_mentions"].users)
                self.assertNotEqual(answer.kwargs["nonce"], card.kwargs["nonce"])

    async def test_uncertain_answer_send_retains_both_generated_parts(self):
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
                         ["Synthetic answer", self.context.state.preview_reply])
