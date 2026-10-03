"""Restricted answers require the asker's deliberate disclosure."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.models import AgentAttachment, AgentTurnState
from elbow_helper.features.agent.wording import ACTION_PRIVATE_ANSWER, ACTION_RESULT_EXPIRED, ACTION_RESULT_OWNER


class PrivateAnswerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.member = SimpleNamespace(id=2, bot=False)
        self.sent = SimpleNamespace(id=7, edit=AsyncMock())
        self.message = SimpleNamespace(id=5, author=self.member, mentions=(),
            channel=SimpleNamespace(id=10), reply=AsyncMock(return_value=self.sent), archive_reply=False)
        self.context = SimpleNamespace(member=self.member, guild=object(), source_message=self.message,
            disclosure_thread_members={}, state=AgentTurnState(source_channels={10, 20}, required_access={"lead"}))
        self.delivery = AgentDeliveryMixin()
        self.delivery.bot = SimpleNamespace(user=SimpleNamespace(id=99))
        self.delivery._conversations = SimpleNamespace(register_reply=Mock())
        self.file = AgentAttachment("result.csv", b"private report")
        self.conversation = object()
        for target in ("delivery", "actions.answer_view"):
            guard = patch(f"elbow_helper.features.agent.{target}.require_evidence_access",
                          new=AsyncMock(return_value={20: object()}))
            guard.start()
            self.addCleanup(guard.stop)

    def interaction(self, identifier=2):
        return SimpleNamespace(id=33, user=SimpleNamespace(id=identifier),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))

    async def notice(self, **kwargs):
        with patch("elbow_helper.features.agent.delivery.can_show", new=AsyncMock(return_value=False)) as audience:
            await self.delivery.send_response(self.message, "Secret answer", None, [self.file],
                self.conversation, context=self.context, **kwargs)
        audience.assert_awaited_once()
        call = self.message.reply.await_args
        self.assertNotIn("Secret", call.args[0])
        self.assertNotIn("files", call.kwargs)
        return call.kwargs["view"]

    async def test_notice_and_private_result_include_no_public_answer(self):
        view = await self.notice()
        self.assertEqual(self.message.reply.await_args.args[0], ACTION_PRIVATE_ANSWER)
        self.assertEqual(view.timeout, 600)
        interaction = self.interaction()
        await view.open_result(interaction)
        call = interaction.response.send_message.await_args
        self.assertEqual(call.args[0], "Secret answer")
        self.assertTrue(call.kwargs["ephemeral"])
        self.assertEqual(call.kwargs["files"][0].filename, "result.csv")

    async def test_post_here_registers_answer_removes_buttons_and_logs_override(self):
        view = await self.notice()
        with self.assertLogs("elbow_helper.features.agent.actions.answer_view", level="INFO") as logs:
            await view.post_here(self.interaction())
        call = self.message.reply.await_args
        self.assertEqual(call.args[0], "Secret answer")
        self.assertNotIn("ephemeral", call.kwargs)
        self.assertEqual(call.kwargs["files"][0].filename, "result.csv")
        self.delivery._conversations.register_reply.assert_called_with(self.conversation, 7)
        self.sent.edit.assert_awaited_once_with(view=None)
        for value in ("requester=2", "channel=10", "sources=[10, 20]", "levels=['lead']"):
            self.assertIn(value, logs.output[0])

    async def test_other_members_cannot_use_either_button(self):
        view = await self.notice()
        for callback in (view.open_result, view.post_here):
            interaction = self.interaction(3)
            await callback(interaction)
            interaction.response.send_message.assert_awaited_once_with(ACTION_RESULT_OWNER, ephemeral=True)

    async def test_lost_access_expires_both_buttons(self):
        for method in ("open_result", "post_here"):
            view = await self.notice()
            interaction = self.interaction()
            with patch("elbow_helper.features.agent.actions.answer_view.require_evidence_access",
                       new=AsyncMock(side_effect=AgentAccessLost())):
                await getattr(view, method)(interaction)
            self.assertTrue(all(item.disabled for item in view.children))
            interaction.response.send_message.assert_awaited_once_with(ACTION_RESULT_EXPIRED, ephemeral=True)

    async def test_qualified_channel_posts_normally(self):
        with patch("elbow_helper.features.agent.delivery.can_show", new=AsyncMock(return_value=True)) as audience:
            await self.delivery.send_response(self.message, "Answer", None, [self.file], context=self.context)
        audience.assert_awaited_once()
        self.assertEqual(self.message.reply.await_args.args[0], "Answer")
        self.assertNotIn("view", self.message.reply.await_args.kwargs)

    async def test_scheduled_notice_mentions_requester_and_lasts_one_hour(self):
        self.message.scheduled_run = True
        view = await self.notice()
        self.assertEqual(view.timeout, 3600)
        self.assertEqual(self.message.reply.await_args.args[0], "<@2> " + ACTION_PRIVATE_ANSWER)
