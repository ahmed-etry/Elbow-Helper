"""Fake changes need one checked owner confirmation."""

from types import SimpleNamespace
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.commands.confirmation import (
    ChangePreview, ConfirmationView, PreparedCommand, preview_text,
)
from elbow_helper.features.agent.commands.outcomes import CommandOutcome
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.models import AgentDelivery, AgentTurnState
from elbow_helper.features.agent.wording import (
    COMMAND_CANCELLED, COMMAND_PREVIEW_CHANGED, COMMAND_PREVIEW_EXPIRED,
    COMMAND_PREVIEW_HEADER, COMMAND_PREVIEW_OWNER,
    COMMAND_PREVIEW_TOO_LONG, COMMAND_PREVIEW_USED,
)


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_then_timeout_keeps_confirmed_preview_text(self):
        proposal, _, _ = self.proposal(1)
        view = self.view((proposal,))
        view.message.content = view.preview
        with patch("elbow_helper.features.agent.commands.confirmation.require_access"), patch(
            "elbow_helper.features.agent.commands.confirmation.require_disclosure_access", new=AsyncMock()
        ):
            await view.confirm(self.interaction(101))
        view.message.edit.reset_mock()
        await view.on_timeout()
        view.message.edit.assert_awaited_once_with(view=view)
        self.assertTrue(all(item.disabled for item in view.children))

    async def test_cancel_then_timeout_keeps_cancelled_text(self):
        proposal, _, _ = self.proposal(1)
        view = self.view((proposal,))
        await view.cancel(self.interaction(101))
        view.message.content = COMMAND_CANCELLED
        await view.on_timeout()
        view.message.edit.assert_awaited_once_with(view=view)
        self.assertTrue(all(item.disabled for item in view.children))

    def proposal(self, value, *, allowed=True):
        check = AsyncMock(return_value=allowed)
        run = AsyncMock(return_value=CommandOutcome("complete", text=f"Result {value}"))
        proposal = PreparedCommand(
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
        view = ConfirmationView(101, tuple(proposals), context)
        view.message = SimpleNamespace(edit=AsyncMock())
        return view

    async def test_multiple_targets_share_one_complete_preview(self):
        proposals = [self.proposal(value)[0] for value in (1, 2, 3)]
        text = preview_text(proposals)
        self.assertTrue(text.startswith(COMMAND_PREVIEW_HEADER))
        self.assertEqual(text.count("Change target"), 3)
        self.assertEqual(text.count(COMMAND_PREVIEW_HEADER), 1)

    async def test_other_member_cannot_confirm_or_cancel(self):
        proposal, check, run = self.proposal(1)
        view = self.view((proposal,))
        other = self.interaction(202)
        await view.confirm(other)
        other.response.send_message.assert_awaited_once_with(COMMAND_PREVIEW_OWNER, ephemeral=True)
        await view.cancel(self.interaction(202))
        check.assert_not_awaited()
        run.assert_not_awaited()
        self.assertFalse(view.used)

    async def test_cancel_and_expiry_never_run_the_command(self):
        proposal, check, run = self.proposal(1)
        view = self.view((proposal,))
        member = self.interaction(101)
        await view.cancel(member)
        member.response.edit_message.assert_awaited_once_with(content=COMMAND_CANCELLED, view=view)
        self.assertTrue(all(item.disabled for item in view.children))
        await view.confirm(self.interaction(101))
        check.assert_not_awaited()
        run.assert_not_awaited()
        expired = self.view((proposal,))
        await expired.on_timeout()
        expired.message.edit.assert_awaited_once_with(content=COMMAND_PREVIEW_EXPIRED, view=expired)
        await expired.confirm(self.interaction(101))
        run.assert_not_awaited()

    async def test_all_preconditions_are_rechecked_before_any_target_runs(self):
        first, first_check, first_run = self.proposal(1)
        second, second_check, second_run = self.proposal(2, allowed=False)
        view = self.view((first, second))
        member = self.interaction(101)
        with (patch("elbow_helper.features.agent.commands.confirmation.require_access"),
              patch("elbow_helper.features.agent.commands.confirmation.require_disclosure_access",
                    new_callable=AsyncMock)):
            await view.confirm(member)
        first_check.assert_awaited_once()
        second_check.assert_awaited_once()
        first_run.assert_not_awaited()
        second_run.assert_not_awaited()
        member.followup.send.assert_awaited_once_with(COMMAND_PREVIEW_CHANGED, ephemeral=True)
        self.assertTrue(view.used)

    async def test_confirm_runs_each_target_once_and_posts_its_result(self):
        first, first_check, first_run = self.proposal(1)
        second, second_check, second_run = self.proposal(2)
        view = self.view((first, second))
        member = self.interaction(101)
        with (patch("elbow_helper.features.agent.commands.confirmation.require_access"),
              patch("elbow_helper.features.agent.commands.confirmation.require_disclosure_access",
                    new_callable=AsyncMock)):
            await asyncio.gather(view.confirm(member), view.confirm(self.interaction(101)))
        first_run.assert_awaited_once()
        second_run.assert_awaited_once()
        self.assertEqual([call.args[0] for call in member.followup.send.await_args_list],
                         ["Result 1", "Result 2"])
        self.assertTrue(view.used)
        self.assertTrue(all(item.disabled for item in view.children))
        view.message.edit.assert_awaited_once_with(view=view)

    async def test_preview_that_does_not_fit_cannot_be_confirmed(self):
        proposal, _, _ = self.proposal("x" * 2000)
        self.assertEqual(preview_text([proposal]), COMMAND_PREVIEW_TOO_LONG)

    async def test_second_click_reports_used_preview(self):
        proposal, _, _ = self.proposal(1)
        view = self.view((proposal,))
        await view.cancel(self.interaction(101))
        member = self.interaction(101)
        await view.confirm(member)
        member.response.send_message.assert_awaited_once_with(COMMAND_PREVIEW_USED, ephemeral=True)

    async def test_failed_command_does_not_run_again(self):
        proposal, _, run = self.proposal(1)
        run.side_effect = ValueError("synthetic failure")
        view = self.view((proposal,))
        member = self.interaction(101)
        with (patch("elbow_helper.features.agent.commands.confirmation.require_access"),
              patch("elbow_helper.features.agent.commands.confirmation.require_disclosure_access",
                    new_callable=AsyncMock)):
            await view.confirm(member)
            await view.confirm(self.interaction(101))
        run.assert_awaited_once()
        self.assertTrue(view.used)

    async def test_private_result_stays_ephemeral_after_confirmation(self):
        proposal, _, run = self.proposal(1)
        run.return_value = CommandOutcome("complete", "private",
                                          private_parts=("synthetic private result",))
        view = self.view((proposal,))
        member = self.interaction(101)
        with (patch("elbow_helper.features.agent.commands.confirmation.require_access"),
              patch("elbow_helper.features.agent.commands.confirmation.require_disclosure_access",
                    new_callable=AsyncMock)):
            await view.confirm(member)
        self.assertTrue(member.followup.send.await_args.kwargs["ephemeral"])
        self.assertNotIn("synthetic private result", str(view.message.edit.await_args))

    async def test_private_text_field_stays_ephemeral_after_confirmation(self):
        proposal, _, run = self.proposal(1)
        run.return_value = CommandOutcome("complete", "private",
                                          text="synthetic private text")
        view = self.view((proposal,))
        member = self.interaction(101)
        with (patch("elbow_helper.features.agent.commands.confirmation.require_access"),
              patch("elbow_helper.features.agent.commands.confirmation.require_disclosure_access",
                    new_callable=AsyncMock)):
            await view.confirm(member)
        self.assertEqual(member.followup.send.await_args.args[0], "synthetic private text")
        self.assertTrue(member.followup.send.await_args.kwargs["ephemeral"])

    async def test_delivery_attaches_one_confirmation_view(self):
        delivery_surface = AgentDeliveryMixin()
        delivery_surface.bot = SimpleNamespace(user=SimpleNamespace(id=999))
        delivery_surface.transcript_archive = None
        sent = SimpleNamespace(id=303)
        message = SimpleNamespace(
            id=101, author=SimpleNamespace(id=202), mentions=[],
            reply=AsyncMock(return_value=sent), channel=SimpleNamespace(),
        )
        state = AgentTurnState()
        state.command_proposals.extend(self.proposal(value)[0] for value in (1, 2))
        context = SimpleNamespace(state=state)
        with patch("elbow_helper.features.agent.delivery.require_disclosure_access",
                   new_callable=AsyncMock):
            await delivery_surface._send_response(
                message, preview_text(state.command_proposals), None,
                delivery=AgentDelivery(), context=context,
            )
        view = message.reply.await_args.kwargs["view"]
        self.assertIsInstance(view, ConfirmationView)
        self.assertEqual(len(view.proposals), 2)
        self.assertEqual(view.owner_id, 202)
        self.assertIs(view.message, sent)

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
        state.command_proposals.append(self.proposal(1)[0])
        state.command_outcomes.append(CommandOutcome(
            "complete", "private", private_parts=("synthetic private result",),
        ))
        context = SimpleNamespace(state=state)
        with patch("elbow_helper.features.agent.delivery.require_disclosure_access",
                   new_callable=AsyncMock):
            await delivery_surface._send_response(
                message, preview_text(state.command_proposals), None,
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

    async def test_cancel_preserves_other_public_result_in_the_message(self):
        proposal, _, _ = self.proposal(1)
        view = self.view((proposal,))
        view.message.content = preview_text([proposal]) + "\n\nSynthetic read result"
        member = self.interaction(101)
        await view.cancel(member)
        member.response.edit_message.assert_awaited_once_with(
            content=COMMAND_CANCELLED + "\n\nSynthetic read result", view=view,
        )
