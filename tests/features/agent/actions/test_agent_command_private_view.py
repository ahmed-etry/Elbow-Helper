"""Private results are visible only to their requester."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.actions.private_view import PrivateResultView
from elbow_helper.features.agent.actions.outcomes import ActionOutcome
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.models import AgentDelivery, AgentTurnState
from elbow_helper.features.agent.models import AgentAttachment
from elbow_helper.features.agent.wording import (
    ACTION_PRIVATE_BUTTON, ACTION_RESULT_EXPIRED, ACTION_RESULT_OWNER,
)


class PrivateCommandViewTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_requester_receives_every_part_privately(self):
        view = PrivateResultView(101, ("synthetic one", "synthetic two"))
        member = self.interaction(101)
        await view.open_result(member)
        member.response.send_message.assert_awaited_once_with("synthetic one", ephemeral=True)
        member.followup.send.assert_awaited_once_with("synthetic two", ephemeral=True)

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

    async def test_expired_view_disables_button_and_refuses_open(self):
        view = PrivateResultView(101, ("synthetic private data",))
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(view.children[0].disabled)
        view.message.edit.assert_awaited_once_with(view=view)
        member = self.interaction(101)
        await view.open_result(member)
        member.response.send_message.assert_awaited_once_with(ACTION_RESULT_EXPIRED, ephemeral=True)

    async def test_delivery_attaches_the_private_view_to_the_public_note(self):
        delivery_surface = AgentDeliveryMixin()
        delivery_surface.bot = SimpleNamespace(user=SimpleNamespace(id=999))
        delivery_surface.transcript_archive = None
        sent = SimpleNamespace(id=303)
        message = SimpleNamespace(
            id=101, author=SimpleNamespace(id=202), mentions=[],
            reply=AsyncMock(return_value=sent),
            channel=SimpleNamespace(),
        )
        state = AgentTurnState()
        state.outcomes.append(ActionOutcome(
            "complete", "private", private_parts=("synthetic private data",),
        ))
        context = SimpleNamespace(state=state)
        from unittest.mock import patch
        with patch("elbow_helper.features.agent.delivery.require_evidence_access",
                   new_callable=AsyncMock):
            await delivery_surface.send_response(
                message, "Result is ready.", None,
                delivery=AgentDelivery(), context=context,
            )
        kwargs = message.reply.await_args.kwargs
        self.assertIsInstance(kwargs["view"], PrivateResultView)
        self.assertEqual(kwargs["view"].owner_id, 202)
        self.assertIs(kwargs["view"].message, sent)
        self.assertNotIn("synthetic private data", str(message.reply.await_args))
