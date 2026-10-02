"""Fake changes need one checked owner confirmation."""

from types import SimpleNamespace
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.preview import (
    ChangePreview, ConfirmationView, PreparedCommand, preview_text,
)
from elbow_helper.features.agent.actions.outcomes import CommandOutcome
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.models import AgentDelivery, AgentTurnState
from elbow_helper.features.agent.wording import (
    COMMAND_CANCELLED, COMMAND_PREVIEW_EXPIRED,
    COMMAND_PREVIEW_HEADER, COMMAND_PREVIEW_OWNER,
    COMMAND_PREVIEW_USED, ACTION_PREVIEW_BLANK,
)


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_then_timeout_keeps_confirmed_preview_text(self):
        proposal, _, _ = self.proposal(1)
        view = self.view((proposal,))
        view.message.content = view.preview
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
        view = ConfirmationView(101, tuple(proposals), context,
                                runner=SimpleNamespace(submit=AsyncMock(return_value="run")))
        view.message = SimpleNamespace(edit=AsyncMock())
        return view

    async def test_multiple_targets_share_one_complete_preview(self):
        proposals = [self.proposal(value)[0] for value in (1, 2, 3)]
        text = preview_text(proposals)
        self.assertTrue(text.startswith(COMMAND_PREVIEW_HEADER))
        self.assertEqual(text.count("Change target"), 3)
        self.assertEqual(text.count(COMMAND_PREVIEW_HEADER), 1)

    async def test_adjacent_actions_share_one_preview_header(self):
        proposals = []
        for value in (1, 2, 3):
            proposal, _, _ = self.proposal(value)
            proposals.append(PreparedCommand(
                proposal.path, proposal.values,
                ChangePreview(proposal.preview.lines, proposal.preview.recheck,
                              summary="Add role"), proposal.run,
            ))
        text = preview_text(proposals)
        self.assertIn("1. Add role: 3 changes", text)
        self.assertEqual(text.count("Add role:"), 1)
        self.assertEqual(text.count("Change target"), 3)

    async def test_multiline_details_keep_the_preview_free_of_blank_lines(self):
        proposal, _, _ = self.proposal(1)
        proposal = PreparedCommand(
            proposal.path, proposal.values,
            ChangePreview(("Details: first\n\nlast",), proposal.preview.recheck),
            proposal.run,
        )
        text = preview_text([proposal])
        self.assertNotIn("\n\n", text)
        self.assertIn(f"Details: first\n{ACTION_PREVIEW_BLANK}\nlast", text)

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

    async def test_button_queues_without_running_preconditions_or_changes(self):
        first, first_check, first_run = self.proposal(1)
        second, second_check, second_run = self.proposal(2, allowed=False)
        view = self.view((first, second))
        member = self.interaction(101)
        await view.confirm(member)
        first_check.assert_not_awaited()
        second_check.assert_not_awaited()
        first_run.assert_not_awaited()
        second_run.assert_not_awaited()
        view.runner.submit.assert_awaited_once_with(
            view.context, view.proposals, confirmer_id=101,
        )
        member.followup.send.assert_not_awaited()
        self.assertTrue(view.used)

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
        view.message.edit.assert_awaited_once_with(view=view)

    async def test_long_preview_is_complete(self):
        proposal, _, _ = self.proposal("x" * 2000)
        self.assertIn("x" * 2000, preview_text([proposal]))

    async def test_blank_preview_line_has_a_fallback(self):
        proposal, _, _ = self.proposal(1)
        proposal = PreparedCommand(proposal.path, proposal.values,
                                   ChangePreview(("",), proposal.preview.recheck), proposal.run)
        self.assertEqual(preview_text([proposal]),
                         COMMAND_PREVIEW_HEADER + "\n1. /synthetic: 1 change\n" + ACTION_PREVIEW_BLANK)

    async def test_second_click_reports_used_preview(self):
        proposal, _, _ = self.proposal(1)
        view = self.view((proposal,))
        await view.cancel(self.interaction(101))
        member = self.interaction(101)
        await view.confirm(member)
        member.response.send_message.assert_awaited_once_with(COMMAND_PREVIEW_USED, ephemeral=True)

    async def test_revised_preview_invalidates_previous_buttons(self):
        proposal, _, run = self.proposal(1)
        view = self.view((proposal,))
        await view.invalidate()
        await view.confirm(self.interaction(101))
        run.assert_not_awaited()
        view.runner.submit.assert_not_awaited()
        self.assertTrue(all(item.disabled for item in view.children[:2]))

    async def test_irreversible_preview_warns_and_groups_same_kind(self):
        proposal, _, _ = self.proposal(1)
        irreversible = PreparedCommand(
            proposal.path, proposal.values, proposal.preview, proposal.run,
            action_class=ActionClass.IRREVERSIBLE,
        )
        self.assertIn("This can't be undone.", preview_text([irreversible]))
        grouped = preview_text([irreversible, irreversible])
        self.assertEqual(grouped.count("This can't be undone."), 1)
        with self.assertRaises(ValueError):
            preview_text([irreversible, proposal])

    async def test_confirmed_preview_cannot_queue_twice(self):
        proposal, _, run = self.proposal(1)
        run.side_effect = ValueError("synthetic failure")
        view = self.view((proposal,))
        member = self.interaction(101)
        await view.confirm(member)
        await view.confirm(self.interaction(101))
        run.assert_not_awaited()
        view.runner.submit.assert_awaited_once()
        self.assertTrue(view.used)
        member.followup.send.assert_not_awaited()

    async def test_private_result_is_not_sent_through_button(self):
        proposal, _, run = self.proposal(1)
        run.return_value = CommandOutcome("complete", "private",
                                          private_parts=("synthetic private result",))
        view = self.view((proposal,))
        member = self.interaction(101)
        await view.confirm(member)
        member.followup.send.assert_not_awaited()
        run.assert_not_awaited()
        self.assertNotIn("synthetic private result", str(view.message.edit.await_args))

    async def test_private_text_is_not_sent_through_button(self):
        proposal, _, run = self.proposal(1)
        run.return_value = CommandOutcome("complete", "private",
                                          text="synthetic private text")
        view = self.view((proposal,))
        member = self.interaction(101)
        await view.confirm(member)
        member.followup.send.assert_not_awaited()
        run.assert_not_awaited()

    async def test_delivery_attaches_one_confirmation_view(self):
        delivery_surface = AgentDeliveryMixin()
        delivery_surface.bot = SimpleNamespace(user=SimpleNamespace(id=999))
        delivery_surface.transcript_archive = None
        delivery_surface._previews = {}
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
        self.assertIs(delivery_surface._previews[sent.id], view)

    async def test_long_preview_puts_buttons_only_on_last_part(self):
        delivery_surface = AgentDeliveryMixin()
        delivery_surface.bot = SimpleNamespace(user=SimpleNamespace(id=999))
        delivery_surface.transcript_archive = None
        delivery_surface._previews = {}
        first = SimpleNamespace(id=303)
        last = SimpleNamespace(id=304)
        channel = SimpleNamespace(send=AsyncMock(return_value=last))
        message = SimpleNamespace(
            id=101, author=SimpleNamespace(id=202), mentions=[],
            reply=AsyncMock(return_value=first), channel=channel,
        )
        state = AgentTurnState()
        state.command_proposals.append(self.proposal("x" * 2000)[0])
        context = SimpleNamespace(state=state)
        with patch("elbow_helper.features.agent.delivery.require_disclosure_access",
                   new_callable=AsyncMock):
            await delivery_surface._send_response(
                message, preview_text(state.command_proposals), None,
                delivery=AgentDelivery(), context=context,
            )
        self.assertNotIn("view", message.reply.await_args.kwargs)
        view = channel.send.await_args.kwargs["view"]
        self.assertIsInstance(view, ConfirmationView)
        self.assertIs(view.message, last)
        self.assertIs(delivery_surface._previews[first.id], view)
        self.assertIs(delivery_surface._previews[last.id], view)
        self.assertEqual((message.reply.await_args.args[0]
                          + channel.send.await_args.args[0]).count("x"), 2000)

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

    async def test_private_panel_opens_from_the_requesters_result_button(self):
        delivery_surface = AgentDeliveryMixin()
        delivery_surface.bot = SimpleNamespace(user=SimpleNamespace(id=999))
        delivery_surface.transcript_archive = None
        sent = SimpleNamespace(id=303)
        message = SimpleNamespace(
            id=101, author=SimpleNamespace(id=202), mentions=[],
            reply=AsyncMock(return_value=sent), channel=SimpleNamespace(),
        )
        panel = AsyncMock()
        state = AgentTurnState()
        state.command_outcomes.append(CommandOutcome(
            "complete", "private", private_panel=panel,
        ))
        context = SimpleNamespace(state=state)
        with patch("elbow_helper.features.agent.delivery.require_disclosure_access",
                   new_callable=AsyncMock):
            await delivery_surface._send_response(
                message, "Result is ready. Open it privately below.", None,
                delivery=AgentDelivery(), context=context,
            )
        view = message.reply.await_args.kwargs["view"]
        await view.open_result(self.interaction(202))
        panel.assert_awaited_once()

    async def test_multiple_private_panels_can_each_be_opened(self):
        first, second = AsyncMock(), AsyncMock()
        from elbow_helper.features.agent.actions.private_view import PrivateCommandView
        view = PrivateCommandView(
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

    async def test_delivery_uses_command_names_for_private_panel_choices(self):
        delivery_surface = AgentDeliveryMixin()
        delivery_surface.bot = SimpleNamespace(user=SimpleNamespace(id=999))
        delivery_surface.transcript_archive = None
        sent = SimpleNamespace(id=303)
        message = SimpleNamespace(
            id=101, author=SimpleNamespace(id=202), mentions=[],
            reply=AsyncMock(return_value=sent), channel=SimpleNamespace(),
        )
        state = AgentTurnState()
        for name in ("/synthetic first", "/synthetic second"):
            state.command_outcomes.append(CommandOutcome(
                "complete", "private", private_panel=AsyncMock(), command_name=name,
            ))
        with patch("elbow_helper.features.agent.delivery.require_disclosure_access",
                   new_callable=AsyncMock):
            await delivery_surface._send_response(
                message, "Result is ready. Open it privately below.", None,
                delivery=AgentDelivery(), context=SimpleNamespace(state=state),
            )
        view = message.reply.await_args.kwargs["view"]
        interaction = self.interaction(202)
        await view.open_result(interaction)
        choices = interaction.response.send_message.await_args.kwargs["view"].children[0].options
        self.assertEqual([choice.label for choice in choices],
                         ["/synthetic first", "/synthetic second"])

    async def test_cancel_preserves_other_public_result_in_the_message(self):
        proposal, _, _ = self.proposal(1)
        view = self.view((proposal,))
        view.message.content = preview_text([proposal]) + "\n\nSynthetic read result"
        member = self.interaction(101)
        await view.cancel(member)
        member.response.edit_message.assert_awaited_once_with(
            content=COMMAND_CANCELLED + "\n\nSynthetic read result", view=view,
        )
