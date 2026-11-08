"""Private results are visible only to their requester."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


from elbow_helper.agent.actions.private_view import PrivateResultView
from elbow_helper.agent.engine.models import AgentAttachment
from elbow_helper.agent.conversation.wording import ACTION_PRIVATE_BUTTON, ACTION_RESULT_EXPIRED, ACTION_RESULT_OWNER


class PrivateCommandViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        guard = patch("elbow_helper.agent.conversation.delivery.assert_answer_scope", new_callable=AsyncMock)
        guard.start()
        self.addCleanup(guard.stop)

    async def test_long_private_results_are_chunked_with_files_only_once(self):
        for with_panel in (False, True):
            with self.subTest(panel=with_panel):
                interaction = self.interaction(101)
                panel = AsyncMock() if with_panel else None
                content = "Synthetic private result. " * 200
                view = PrivateResultView(101, (content,),
                    (AgentAttachment("synthetic.txt", b"secret"),), panel=panel)
                await view.open_result(interaction)
                calls = (interaction.followup.send.await_args_list if with_panel else
                         [interaction.response.send_message.await_args,
                          *interaction.followup.send.await_args_list])
                self.assertGreater(len(calls), 1)
                self.assertTrue(all(len(call.args[0]) <= 2000 for call in calls))
                self.assertTrue(all(call.kwargs["ephemeral"] for call in calls))
                self.assertEqual(len(calls[0].kwargs["files"]), 1)
                self.assertTrue(all("files" not in call.kwargs for call in calls[1:]))

    def interaction(self, member_id):
        return SimpleNamespace(
            user=SimpleNamespace(id=member_id),
            response=SimpleNamespace(send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_other_member_cannot_open_result(self):
        view = PrivateResultView(101, ("synthetic private data",))
        self.assertEqual(view.children[0].label, ACTION_PRIVATE_BUTTON)
        other = self.interaction(202)
        await view.open_result(other)
        other.response.send_message.assert_awaited_once_with(
            ACTION_RESULT_OWNER, ephemeral=True,
        )
        self.assertNotIn("synthetic private data", str(other.response.send_message.await_args))


    async def test_private_attachment_is_sent_only_on_owner_click(self):
        view = PrivateResultView(101, (), (AgentAttachment("synthetic.txt", b"secret"),))
        other = self.interaction(202)
        await view.open_result(other)
        self.assertNotIn("files", other.response.send_message.await_args.kwargs)
        member = self.interaction(101)
        await view.open_result(member)
        sent = member.response.send_message.await_args.kwargs
        self.assertTrue(sent["ephemeral"])
        self.assertEqual(len(sent["files"]), 1)

    async def test_private_image_result_suppresses_all_text_parts(self):
        for with_panel in (False, True):
            with self.subTest(panel=with_panel):
                member = self.interaction(101)
                view = PrivateResultView(101, ('Synthetic caption.', 'More caption.'),
                    (AgentAttachment('synthetic.png', b'media'),),
                    panel=AsyncMock() if with_panel else None)
                self.addCleanup(view.stop)
                await view.open_result(member)
                sender = member.followup.send if with_panel else member.response.send_message
                sender.assert_awaited_once()
                self.assertIsNone(sender.await_args.args[0])
                self.assertEqual(len(sender.await_args.kwargs['files']), 1)
                if not with_panel:
                    member.followup.send.assert_not_awaited()

    async def test_private_media_link_result_keeps_its_text_parts(self):
        member = self.interaction(101)
        image = 'https://example.invalid/synthetic.png'
        view = PrivateResultView(101, ('Synthetic caption.', image, 'More caption.'))
        self.addCleanup(view.stop)
        await view.open_result(member)
        member.response.send_message.assert_awaited_once_with('Synthetic caption.', ephemeral=True)
        self.assertEqual([call.args[0] for call in member.followup.send.await_args_list],
                         [image, 'More caption.'])

    async def test_expired_view_disables_button_and_refuses_open(self):
        view = PrivateResultView(101, ("synthetic private data",))
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(view.children[0].disabled)
        view.message.edit.assert_awaited_once_with(view=view)
        member = self.interaction(101)
        await view.open_result(member)
        member.response.send_message.assert_awaited_once_with(ACTION_RESULT_EXPIRED, ephemeral=True)
