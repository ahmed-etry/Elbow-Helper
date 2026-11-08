"""Fake changes need one checked owner confirmation."""

from types import SimpleNamespace
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.agent.actions.preview import ConfirmationView, preview_text
from elbow_helper.agent.actions.contracts import ChangePreview, PreparedAction
from elbow_helper.agent.actions.outcomes import ActionOutcome
from elbow_helper.agent.actions.private_view import PrivateResultView
from elbow_helper.agent.delivery import AgentDeliveryMixin
from elbow_helper.agent.models import AgentDelivery, AgentTurnState
from elbow_helper.agent.wording import ACTION_CANCELLED, ACTION_PREVIEW_EXPIRED, ACTION_PREVIEW_OWNER


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        guard = patch("elbow_helper.agent.delivery.require_evidence_access", new_callable=AsyncMock)
        guard.start()
        self.addCleanup(guard.stop)

    async def test_deleted_preview_timeout_is_debug_and_still_expires(self):
        proposal, _, _ = self.proposal(1)
        view = self.view((proposal,))
        view.message.edit.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"),
            {"code": 10008, "message": "Unknown Message"},
        )
        with self.assertLogs("elbow_helper.agent.actions.preview", level="DEBUG") as logs:
            await view.on_timeout()
        self.assertEqual([record.levelname for record in logs.records], ["DEBUG"])
        self.assertTrue(view.expired)
        self.assertTrue(view.is_finished())
        interaction = self.interaction(101)
        await view.confirm(interaction)
        interaction.response.send_message.assert_awaited_once_with(ACTION_PREVIEW_EXPIRED, ephemeral=True)
        view.runner.submit.assert_not_awaited()


    async def test_confirm_hands_a_preview_only_message_to_the_run(self):
        proposal, _, _ = self.proposal(1)
        view = self.view((proposal,))
        view.message.content = view.preview
        order = []
        view.message.edit.side_effect = lambda **_: order.append("disabled")
        view.runner.submit.side_effect = lambda *_, **__: order.append("queued")
        await view.confirm(self.interaction(101))
        view.runner.submit.assert_awaited_once_with(
            view.context, view.proposals, confirmer_id=101, progress_message=view.message,
        )
        self.assertEqual(order, ["queued"])
        self.assertTrue(view.is_finished())


    async def test_failed_queue_removes_controls_from_a_reusable_card(self):
        proposal, _, run = self.proposal(1)
        view = self.view((proposal,))
        view.message.content = view.preview
        view.context.delivery_channel = SimpleNamespace(send=AsyncMock())
        view.runner.submit.side_effect = RuntimeError("Synthetic queue failure")
        with self.assertLogs("elbow_helper.agent.actions.preview", level="ERROR"):
            await view.confirm(self.interaction(101))
        view.message.edit.assert_awaited_once_with(view=None)
        self.assertTrue(view.is_finished())
        run.assert_not_awaited()
        view.message.edit.reset_mock()
        await view.on_timeout()
        view.message.edit.assert_not_awaited()

    def proposal(self, value, *, allowed=True):
        check = AsyncMock(return_value=allowed)
        run = AsyncMock(return_value=ActionOutcome("complete", text=f"Result {value}"))
        proposal = PreparedAction(
            "/synthetic", {"target": value},
            ChangePreview((f"Change target {value}",), check), run,
        )
        return proposal, check, run

    def interaction(self, member_id):
        return SimpleNamespace(
            user=SimpleNamespace(id=member_id),
            response=SimpleNamespace(send_message=AsyncMock(),
                                     edit_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def view(self, proposals):
        context = SimpleNamespace(guild=object(),
                                  source_message=SimpleNamespace(channel=object()))
        view = ConfirmationView(101, tuple(proposals), context,
                                runner=SimpleNamespace(submit=AsyncMock(return_value="run")))
        view.message = SimpleNamespace(edit=AsyncMock())
        return view


    async def test_other_member_cannot_confirm_or_cancel(self):
        proposal, check, run = self.proposal(1)
        view = self.view((proposal,))
        other = self.interaction(202)
        await view.confirm(other)
        other.response.send_message.assert_awaited_once_with(ACTION_PREVIEW_OWNER, ephemeral=True)
        await view.cancel(self.interaction(202))
        check.assert_not_awaited()
        run.assert_not_awaited()
        self.assertFalse(view.used)

    async def test_cancel_and_expiry_never_run_the_command(self):
        proposal, check, run = self.proposal(1)
        view = self.view((proposal,))
        member = self.interaction(101)
        await view.cancel(member)
        member.response.edit_message.assert_awaited_once_with(content=ACTION_CANCELLED, view=None)
        self.assertTrue(all(item.disabled for item in view.children))
        await view.confirm(self.interaction(101))
        check.assert_not_awaited()
        run.assert_not_awaited()
        expired = self.view((proposal,))
        await expired.on_timeout()
        expired.message.edit.assert_awaited_once_with(content=ACTION_PREVIEW_EXPIRED, view=None)
        await expired.confirm(self.interaction(101))
        run.assert_not_awaited()


    async def test_two_clicks_queue_one_run_and_report_nothing_through_button(self):
        first, first_check, first_run = self.proposal(1)
        second, second_check, second_run = self.proposal(2)
        view = self.view((first, second))
        member = self.interaction(101)
        await asyncio.gather(view.confirm(member), view.confirm(self.interaction(101)))
        first_run.assert_not_awaited()
        second_run.assert_not_awaited()
        first_check.assert_not_awaited()
        second_check.assert_not_awaited()
        view.runner.submit.assert_awaited_once()
        member.followup.send.assert_not_awaited()
        self.assertTrue(view.used)
        self.assertTrue(all(item.disabled for item in view.children))
        view.message.edit.assert_awaited_once_with(view=None)

    async def test_long_preview_is_complete(self):
        proposal, _, _ = self.proposal("x" * 2000)
        self.assertIn("x" * 2000, preview_text([proposal]))


    async def test_revised_preview_invalidates_previous_buttons(self):
        proposal, _, run = self.proposal(1)
        view = self.view((proposal,))
        await view.invalidate()
        await view.confirm(self.interaction(101))
        run.assert_not_awaited()
        view.runner.submit.assert_not_awaited()
        self.assertTrue(all(item.disabled for item in view.children[:2]))
        view.message.edit.assert_awaited_once_with(view=None)
        self.assertTrue(view.is_finished())


    async def test_mixed_private_result_remains_available_after_cancel(self):
        delivery_surface = AgentDeliveryMixin()
        delivery_surface.bot = SimpleNamespace(user=SimpleNamespace(id=999))
        delivery_surface.transcript_archive = None
        sent = SimpleNamespace(id=303)
        message = SimpleNamespace(
            id=101, author=SimpleNamespace(id=202), mentions=[],
            reply=AsyncMock(return_value=sent), channel=SimpleNamespace(),
        )
        state = AgentTurnState()
        state.proposed_changes.append(self.proposal(1)[0])
        state.outcomes.append(ActionOutcome(
            "complete", "private", private_parts=("synthetic private result",),
        ))
        context = SimpleNamespace(state=state)
        with patch("elbow_helper.agent.delivery.assert_answer_scope",
                   new_callable=AsyncMock):
            await delivery_surface.send_response(
                message, preview_text(state.proposed_changes), None,
                delivery=AgentDelivery(), context=context,
            )
        view = message.reply.await_args.kwargs["view"]
        self.assertEqual(len(view.children), 3)
        await view.cancel(self.interaction(202))
        self.assertFalse(view.children[2].disabled)
        requester = self.interaction(202)
        await view.children[2].callback(requester)
        requester.response.send_message.assert_awaited_once_with(
            "synthetic private result", ephemeral=True,
        )


    async def test_multiple_private_panels_can_each_be_opened(self):
        first, second = AsyncMock(), AsyncMock()
        view = PrivateResultView(
            202, (), panels=(first, second),
            panel_labels=("/synthetic first", "/synthetic second"),
        )
        interaction = self.interaction(202)
        await view.open_result(interaction)
        selection = interaction.response.send_message.await_args.kwargs["view"]
        selector = selection.children[0]
        self.assertEqual([option.label for option in selector.options],
                         ["/synthetic first", "/synthetic second"])
        selector._values = ["1"]
        await selector.callback(self.interaction(202))
        first.assert_not_awaited()
        second.assert_awaited_once()
