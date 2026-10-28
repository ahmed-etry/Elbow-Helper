"""Synthetic private-answer disclosure; no live Discord or model calls."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.access import ACCESS_VIEW_AUDIT_LOG
from elbow_helper.agent.actions.answer_view import PostAnswerView
from elbow_helper.agent.actions.combined_reply import CombinedReplyView
from elbow_helper.agent.actions.contracts import ChangePreview, PreparedAction
from elbow_helper.agent.actions.preview import preview_text
from elbow_helper.agent.conversation.state import (
    ConversationRecord, ConversationStore, ConversationTurn,
)
from elbow_helper.agent.models import AgentAttachment, AgentDelivery, AgentTurnState
from elbow_helper.agent.research.history import read_message_context
from elbow_helper.agent.wording import (
    ACTION_CANCELLED, ACTION_POST_HERE_BUTTON, ACTION_PRIVATE_BUTTON,
    ACTION_RESULT_EXPIRED, ACTION_RESULT_OWNER, ACTION_UNAVAILABLE,
    AGENT_PRIVATE_CHECK_BUTTON, AGENT_PRIVATE_OFFER, AGENT_SCOPE_EMPTY,
    LONG_REPLY_FILENAME,
)
from .. import test_evidence_scope as fixtures


class PostHereTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ScopeTests.setUp(self)
        self.message.channel = self.member_chat
        self.delivery._archive_reply = AsyncMock()
        self.delivery._conversations = ConversationStore()
        self.delivery.persistence = None
        self.sent = SimpleNamespace(id=999, content="", files=[])

        async def edit(**changes):
            self.sent.content = changes.get("content", self.sent.content)
            self.sent.view = changes.get("view", getattr(self.sent, "view", None))
            if "attachments" in changes:
                self.sent.files = [(file.filename, file.fp.getvalue()) for file in changes["attachments"]]
            return self.sent

        self.sent.edit = AsyncMock(side_effect=edit)

        async def reply(content, **options):
            self.sent.content = content
            self.sent.view = options.get("view")
            self.sent.files = [(file.filename, file.fp.getvalue()) for file in options.get("files", ())]
            return self.sent

        self.message.reply.side_effect = reply

    def interaction(self, member=None):
        return SimpleNamespace(
            user=member or self.asker,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))),
            edit_original_response=AsyncMock(),
        )

    async def deliver(self, public="Partial facts", full="Full recruitment and support facts",
                      attachments=(), conversation=None):
        for channel_id in (200, 201):
            self.assertIn("error", await read_message_context(
                self.context, {"channel_id": channel_id, "message_id": 123},
            ))
        self.context.state.scope_audit.offered = True
        private = replace(self.context, state=AgentTurnState(), private_pass=True)
        for channel_id in (200, 201):
            self.assertIn("messages", await read_message_context(
                private, {"channel_id": channel_id, "message_id": 123},
            ))
        self.context.state.private_answer_run = AsyncMock(return_value=(full, attachments, private))
        active = AgentDelivery()
        await self.delivery.send_response(
            self.message, public, None, context=self.context, delivery=active, conversation=conversation,
        )
        root = self.message.reply.await_args.kwargs["view"]
        self.addCleanup(root.stop)
        parent = root.views["answer"] if isinstance(root, CombinedReplyView) else root
        check = self.interaction()
        await parent.check_privately(check)
        self.assertTrue(all(call.kwargs["ephemeral"] for call in check.followup.send.await_args_list))
        post_view = check.followup.send.await_args_list[0].kwargs["view"]
        self.assertIsInstance(post_view, PostAnswerView)
        self.addCleanup(post_view.stop)
        self.assertEqual(post_view.children[0].label, ACTION_POST_HERE_BUTTON)
        return parent, post_view, private, active

    async def test_post_replaces_partial_and_scope_empty_without_another_public_message(self):
        for public in ("Partial facts", AGENT_SCOPE_EMPTY):
            with self.subTest(public=public):
                parent, post, private, active = await self.deliver(public=public)
                self.assertIn(AGENT_PRIVATE_OFFER, self.sent.content)
                self.assertEqual(parent.children[0].label, AGENT_PRIVATE_CHECK_BUTTON)
                before = self.message.reply.await_count
                with self.assertLogs("elbow_helper.agent.disclosure", level="INFO") as audit:
                    await post.post_here(self.interaction())
                self.assertEqual(self.sent.content, "Full recruitment and support facts")
                self.assertIsNone(self.sent.view)
                self.assertEqual(self.message.reply.await_count, before)
                self.member_chat.send.assert_not_awaited()
                self.assertEqual(active.message_ids, [999])
                self.assertEqual(active.text_parts, [self.sent.content])
                self.assertNotIn(200, self.context.state.source_channels)
                self.assertIn(200, private.state.source_channels)
                self.assertIn("excluded_classes=[('per_person', 1), ('role_scope', 1)]", audit.output[0])
                self.assertIn("request=1000 requester=10 channel=101 reply=999", audit.output[0])

    async def test_only_asker_can_post(self):
        parent, post, _, _ = await self.deliver()
        interaction = self.interaction(self.peer)
        await post.post_here(interaction)
        interaction.response.send_message.assert_awaited_once_with(ACTION_RESULT_OWNER, ephemeral=True)
        interaction.response.defer.assert_not_awaited()
        self.sent.edit.assert_not_awaited()
        self.assertFalse(parent.posted)

    async def test_private_image_check_and_public_post_have_no_caption(self):
        attachment = AgentAttachment('synthetic.png', b'media')
        parent, post, _, active = await self.deliver(
            full='Synthetic fallback.', attachments=(attachment,))
        check = self.interaction()
        await parent.check_privately(check)
        call, = check.followup.send.await_args_list
        self.assertIsNone(call.args[0])
        self.assertEqual(len(call.kwargs['files']), 1)
        extra = call.kwargs.get('view')
        if extra is not None:
            self.addCleanup(extra.stop)
        await post.post_here(self.interaction())
        self.assertEqual(self.sent.content, '')
        self.assertEqual(self.sent.files, [('synthetic.png', b'media')])
        self.assertEqual(active.text_parts, [''])

    async def test_private_image_post_keeps_confirmation_on_a_separate_message(self):
        action = PreparedAction('synthetic_change', {},
            ChangePreview(('Synthetic change',), AsyncMock(return_value=True)), AsyncMock())
        self.context.state.proposed_changes.append(action)
        self.context.state.preview_reply = preview_text([action])
        conversation = self.delivery._conversations.create(1, 101, 1000)
        _, post, _, active = await self.deliver(full='Synthetic fallback.',
            attachments=(AgentAttachment('synthetic.png', b'media'),), conversation=conversation)
        conversation.append(ConversationTurn('', frozenset(), record=ConversationRecord(
            request_message_id=1000, member_id=10, created_at='2026-10-22',
            question='Synthetic mixed request', generated_answer=self.sent.content,
            delivered_answer=self.sent.content, local_context='', evidence=(), report_ids=(),
            reply_ids=(999,), delivery_complete=True, generated_parts=tuple(active.generated_parts),
        )))
        combined = self.sent.view
        self.assertIsInstance(combined, CombinedReplyView)
        preview = combined.views['preview']
        self.member_chat.send.return_value = SimpleNamespace(id=1001)
        await post.post_here(self.interaction())
        call, = self.member_chat.send.await_args_list
        self.assertIsNone(call.args[0])
        self.assertEqual(len(call.kwargs['files']), 1)
        self.assertNotIn('Synthetic fallback.', self.sent.content)
        self.assertEqual(self.sent.files, [])
        self.assertIs(combined.views['preview'], preview)
        self.assertIsNone(combined.views['answer'])
        self.assertEqual(active.message_ids, [999, 1001])
        self.assertEqual(active.text_parts, [self.sent.content, ''])
        self.assertEqual(conversation.turns[-1].record.reply_ids, (999, 1001))
        self.assertEqual(conversation.turns[-1].record.generated_parts, (self.sent.content, ''))
        await preview.cancel(self.interaction())
        self.assertIn(ACTION_CANCELLED, self.sent.content)

    async def test_audit_counts_multiple_exclusions_in_each_class(self):
        self.context.state.scope_audit.excluded["knowledge:synthetic"] = "role_scope"
        _, post, _, _ = await self.deliver()
        with self.assertLogs("elbow_helper.agent.disclosure", level="INFO") as audit:
            await post.post_here(self.interaction())
        self.assertIn("excluded_classes=[('per_person', 1), ('role_scope', 2)]", audit.output[0])

    async def test_revoked_role_channel_membership_and_bot_access_block_cached_answer(self):
        changes = (
            lambda: self.asker.roles.clear(),
            lambda: self.personal.viewers.remove(self.asker.id),
            lambda: self.member_chat.viewers.remove(self.asker.id),
            lambda: self.ticket.viewers.remove(self.bot_member.id),
            lambda: self.guild.members.remove(self.asker),
        )
        for change in changes:
            with self.subTest(change=change):
                self.setUp()
                parent, post, _, _ = await self.deliver()
                change()
                interaction = self.interaction()
                await post.post_here(interaction)
                interaction.followup.send.assert_awaited_once_with(ACTION_RESULT_EXPIRED, ephemeral=True)
                self.sent.edit.assert_not_awaited()
                self.assertFalse(parent.posted)

    async def test_simultaneous_buttons_share_one_edit_and_one_audit_event(self):
        parent, first, _, _ = await self.deliver()
        check = self.interaction()
        await parent.check_privately(check)
        second = check.followup.send.await_args.kwargs["view"]
        self.addCleanup(second.stop)
        with self.assertLogs("elbow_helper.agent.disclosure", level="INFO") as audit:
            await asyncio.gather(first.post_here(self.interaction()), second.post_here(self.interaction()))
        self.sent.edit.assert_awaited_once()
        self.assertEqual(len(audit.output), 1)
        self.context.state.private_answer_run.assert_awaited_once()

    async def test_uncertain_post_blocks_another_attempt_without_recording_override(self):
        parent, post, _, _ = await self.deliver()
        edit = self.sent.edit.side_effect
        self.sent.edit.side_effect = RuntimeError("Synthetic edit failure")
        interaction = self.interaction()
        with self.assertLogs("elbow_helper.agent.actions.answer_view", level="ERROR"), \
                self.assertNoLogs("elbow_helper.agent.disclosure", level="INFO"):
            await post.post_here(interaction)
        self.assertTrue(parent.posted)
        interaction.followup.send.assert_awaited_once_with(
            "I couldn't confirm that the answer was posted.", ephemeral=True)
        self.sent.edit.side_effect = edit
        with self.assertNoLogs("elbow_helper.agent.disclosure", level="INFO"):
            await post.post_here(self.interaction())
        self.sent.edit.assert_awaited_once()
        self.assertTrue(parent.posted)

    async def test_public_expiry_cannot_restore_old_controls_during_post(self):
        parent, post, _, _ = await self.deliver()
        editing, release = asyncio.Event(), asyncio.Event()
        edit = self.sent.edit.side_effect

        async def paused_edit(**changes):
            editing.set()
            await release.wait()
            return await edit(**changes)

        self.sent.edit.side_effect = paused_edit
        with self.assertLogs("elbow_helper.agent.disclosure", level="INFO"):
            posting = asyncio.create_task(post.post_here(self.interaction()))
            await editing.wait()
            expiry = asyncio.create_task(parent.on_timeout())
            await asyncio.sleep(0)
            self.assertFalse(expiry.done())
            release.set()
            await asyncio.gather(posting, expiry)
        self.sent.edit.assert_awaited_once()
        self.assertIsNone(self.sent.view)

    async def test_expired_private_or_public_controls_cannot_post(self):
        for expire_parent in (False, True):
            with self.subTest(expire_parent=expire_parent):
                parent, post, _, _ = await self.deliver()
                await (parent.on_timeout() if expire_parent else post.on_timeout())
                self.sent.edit.reset_mock()
                interaction = self.interaction()
                await post.post_here(interaction)
                interaction.followup.send.assert_awaited_once_with(ACTION_RESULT_EXPIRED, ephemeral=True)
                self.sent.edit.assert_not_awaited()

    async def test_long_public_and_private_answers_use_the_same_message_and_full_file(self):
        full = "Full private facts\n" * 250
        _, post, _, _ = await self.deliver(
            public="Partial facts\n" * 200, full=full,
            attachments=(AgentAttachment("full.csv", b"Synthetic full export"),),
        )
        self.assertEqual(self.sent.files[0][0], LONG_REPLY_FILENAME)
        with self.assertLogs("elbow_helper.agent.disclosure", level="INFO"):
            await post.post_here(self.interaction())
        self.assertEqual(self.sent.content, "")
        self.assertEqual(self.sent.files, [
            ("full.csv", b"Synthetic full export"), (LONG_REPLY_FILENAME, full.encode("utf-8")),
        ])
        self.message.reply.assert_awaited_once()
        self.member_chat.send.assert_not_awaited()

    async def test_post_preserves_private_command_result_controls(self):
        self.context.state.outcomes.append(SimpleNamespace(
            visibility="private", private_parts=("Private command result",), attachments=(),
            private_panel=None,
        ))
        _, post, _, _ = await self.deliver(full="Full facts, <@11> @everyone")
        with self.assertLogs("elbow_helper.agent.disclosure", level="INFO"):
            await post.post_here(self.interaction())
        self.addCleanup(self.sent.view.stop)
        self.assertEqual([item.label for item in self.sent.view.children], [ACTION_PRIVATE_BUTTON])
        self.assertEqual(self.sent.view.parts, ("Private command result",))
        self.assertNotIn("Private command result", self.sent.content)
        mentions = self.sent.edit.await_args.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.roles)
        self.assertFalse(mentions.replied_user)

    async def test_combined_preview_survives_short_and_long_full_answer_edits(self):
        for full in ("Full facts", "Full facts\n" * 250):
            with self.subTest(full_length=len(full)):
                self.setUp()
                self.message.archive_reply = True
                self.delivery._archive_reply_delivery = AsyncMock()
                action = PreparedAction(
                    "synthetic_change", {}, ChangePreview(("Synthetic change",), AsyncMock(return_value=True)),
                    AsyncMock(),
                )
                self.context.state.proposed_changes.append(action)
                self.context.state.preview_reply = preview_text([action])
                _, post, _, _ = await self.deliver(full=full)
                combined = self.sent.view
                self.assertIsInstance(combined, CombinedReplyView)
                preview = combined.views["preview"]
                with self.assertLogs("elbow_helper.agent.disclosure", level="INFO"):
                    await post.post_here(self.interaction())
                self.assertEqual(combined.parts["preview"], preview_text([action]))
                self.assertIs(combined.views["preview"], preview)
                self.assertNotIn(AGENT_PRIVATE_OFFER, self.sent.content)
                if len(full) > 2000:
                    self.assertEqual(self.sent.files, [(LONG_REPLY_FILENAME, full.encode("utf-8"))])
                else:
                    self.assertIn(full, self.sent.content)
                self.delivery._archive_reply_delivery.assert_awaited_once_with(
                    1000, self.sent.id, full, (LONG_REPLY_FILENAME,) if len(full) > 2000 else (),
                )
                await preview.cancel(self.interaction())
                self.assertIn(ACTION_CANCELLED, self.sent.content)
                self.assertIsNone(combined.views["answer"])
                self.message.reply.assert_awaited_once()

    async def test_post_updates_archived_and_retained_reply_content(self):
        self.message.archive_reply = True
        conversation = self.delivery._conversations.create(1, 101, 1000)
        _, post, _, active = await self.deliver(conversation=conversation)
        previous = self.sent.content
        conversation.append(ConversationTurn(
            previous, frozenset(), record=ConversationRecord(
                request_message_id=1000, member_id=10, created_at="2026-10-22",
                question="Synthetic question", generated_answer=previous, delivered_answer=previous,
                local_context="", evidence=(), report_ids=(), reply_ids=(999,),
                delivery_complete=True, generated_parts=tuple(active.generated_parts),
            ),
        ))
        with self.assertLogs("elbow_helper.agent.disclosure", level="INFO"):
            await post.post_here(self.interaction())
        self.delivery._archive_reply.assert_awaited_with(
            1000, 999, self.sent.content, previous_content=previous,
            attachment_names=(),
        )
        self.assertEqual(conversation.turns[-1].record.delivered_answer, self.sent.content)
        self.assertEqual(conversation.turns[-1].record.reply_ids, (999,))

    async def test_split_preview_has_no_private_answer_override(self):
        action = PreparedAction(
            "synthetic_change", {}, ChangePreview(("Synthetic change",), AsyncMock(return_value=True)),
            AsyncMock(),
        )
        self.context.state.proposed_changes.append(action)
        self.context.state.preview_reply = preview_text([action])
        # The private check needs excluded evidence that the asker can access.
        result = await read_message_context(self.context, {"channel_id": 200, "message_id": 123})
        self.assertIn("error", result)
        self.context.state.scope_audit.offered = True
        self.context.state.private_answer_run = AsyncMock()
        await self.delivery.send_response(
            self.message, "Partial facts\n" * 200, None, context=self.context,
        )
        calls = self.message.reply.await_args_list
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.addCleanup(call.kwargs["view"].stop)
        self.assertEqual(calls[0].kwargs["view"].children[0].label, AGENT_PRIVATE_CHECK_BUTTON)
        self.assertNotIn(AGENT_PRIVATE_OFFER, calls[1].args[0])
        self.assertNotIn(AGENT_PRIVATE_CHECK_BUTTON, [item.label for item in calls[1].kwargs["view"].children])
        self.context.state.private_answer_run.assert_not_awaited()

    async def test_audit_log_answers_remain_private_even_with_forged_callback(self):
        parent, original, private, _ = await self.deliver()
        private.state.required_access.add(ACCESS_VIEW_AUDIT_LOG)
        for member in (self.asker, self.bot_member):
            member.guild_permissions = SimpleNamespace(view_audit_log=True)
        check = self.interaction()
        await parent.check_privately(check)
        self.assertNotIn("view", check.followup.send.await_args.kwargs)
        interaction = self.interaction()
        await original.post_here(interaction)
        interaction.followup.send.assert_awaited_once_with(ACTION_UNAVAILABLE, ephemeral=True)
        self.sent.edit.assert_not_awaited()
