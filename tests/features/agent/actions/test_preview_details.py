"""Hidden change details remain private while confirmation stays available."""

from types import SimpleNamespace
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.configuration.roles import CORE, LEAD
from elbow_helper.features.agent.access import ACCESS_LEAD
from elbow_helper.features.agent.actions.contracts import ActionClass, ChangePreview, PreparedAction
from elbow_helper.features.agent.actions.details import prepare_preview
from elbow_helper.features.agent.actions.outcomes import ActionOutcome
from elbow_helper.features.agent.actions.preview import ConfirmationView, preview_text
from elbow_helper.features.agent.actions.runner import AgentActionRunner
from elbow_helper.features.agent.actions.store import AgentActionRepository
from elbow_helper.features.agent.actions.private_view import PrivateResultView
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.conversation.turns import AgentTurnMixin
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.scheduled.tools import prepare_manage, prepare_save
from elbow_helper.features.agent.wording import ACTION_PREVIEW_DETAILS_BUTTON, ACTION_PREVIEW_DETAILS_HIDDEN
from tests.features.agent.test_agent_audiences import Channel, Member, Role


SECRET = "Synthetic private content"


class PreviewDetailTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        lead, core = Role(next(iter(LEAD))), Role(next(iter(CORE)))
        self.member = Member(2, (lead, core))
        self.other = Member(3, ())
        self.bot_member = Member(99, (lead,))
        members = {member.id: member for member in (self.member, self.other, self.bot_member)}
        self.guild = SimpleNamespace(
            id=1, chunked=True, members=list(members.values()), me=self.bot_member,
            get_member=members.get, default_role=Role(1), roles=[Role(1), lead, core],
        )
        self.destination = Channel(10, self.guild, members)
        self.destination.mention = "<#10>"
        self.source = Channel(20, self.guild, {2, 99})
        self.guild.get_channel_or_thread = {10: self.destination, 20: self.source}.get
        self.message = SimpleNamespace(
            id=30, guild=self.guild, channel=self.destination, author=self.member,
            mentions=(), reply=AsyncMock(return_value=SimpleNamespace(id=31, edit=AsyncMock())),
            archive_reply=False,
        )
        self.context = AgentRequestContext(
            bot=SimpleNamespace(user=self.bot_member), guild=self.guild, member=self.member,
            source_message=self.message, account_links=None, clan_health=None, message_search=None,
        )
        self.context.state.source_channels.add(10)
        self.action = PreparedAction(
            "synthetic_change", {"target": 20},
            ChangePreview(
                ("Change target",), AsyncMock(return_value=True), summary="Synthetic change",
                details=(SECRET,), detail_sources=frozenset({20}),
                detail_access=frozenset({ACCESS_LEAD}),
            ),
            AsyncMock(return_value=ActionOutcome("complete", text=SECRET)),
        )
        self.context.state.proposed_changes.append(self.action)

    def interaction(self, member=None):
        return SimpleNamespace(
            user=member or self.member,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_saved_rule_management_keeps_original_detail_sources(self):
        with TemporaryDirectory() as directory:
            repository = AgentActionRepository(Path(directory) / "actions.sqlite3")
            self.context = replace(self.context, action_repository=repository)
            self.context.state.proposed_changes.clear()
            self.context.state.source_channels = {20}
            self.context.state.required_access = {ACCESS_LEAD}
            self.message.channel = self.source
            values = {"kind": "request", "request": SECRET, "timezone": "UTC",
                      "schedule": {"kind": "once"}, "destination_channel_id": 10}
            run_at = datetime.now(timezone.utc) + timedelta(days=1)
            with (patch("elbow_helper.features.agent.scheduled.tools.resolve_channel",
                        new=AsyncMock(return_value=self.destination)),
                  patch("elbow_helper.features.agent.scheduled.tools.check_post_access"),
                  patch("elbow_helper.features.agent.scheduled.tools._schedule",
                        return_value=(run_at,))):
                result = await prepare_save(self.context, values, registry_factory=dict)
            action = self.context.state.proposed_changes.pop()
            self.assertNotIn(SECRET, str(result))
            self.assertNotIn(SECRET, "\n".join(action.preview.lines))
            self.assertIn(SECRET, "\n".join(action.preview.details))
            await action.run()
            saved = repository.list_standing(requester_id=2)[0]
            self.message.channel = self.destination
            self.context.state.source_channels = {10}
            self.context.state.required_access.clear()
            result = await prepare_manage(self.context, {
                "kind": "request", "id": saved["request_id"], "operation": "pause",
            })
            self.assertNotIn(SECRET, str(result))
            await prepare_preview(self.context)
            proposal = self.context.state.proposed_changes[0]
            self.assertTrue(proposal.details_hidden)
            self.assertEqual(proposal.preview.detail_sources, frozenset({20}))
            self.assertEqual(proposal.preview.detail_access, frozenset({ACCESS_LEAD}))
            self.assertNotIn(SECRET, preview_text([proposal]))
            view = ConfirmationView(2, (proposal,), self.context)
            interaction = self.interaction()
            await view.show_details(interaction)
            self.assertIn(SECRET, interaction.response.send_message.await_args.args[0])
            self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_retained_visible_preview_keeps_detail_access_requirements(self):
        recorder = SimpleNamespace(_commit_reports=MagicMock(),
                                   _refresh_history_checkpoint=MagicMock(), persistence=None)
        self.message.created_at = datetime.now(timezone.utc)
        member = SimpleNamespace(id=2, display_name="Member")
        delivery = SimpleNamespace(attempted_nonces=(1,), complete=True, unknown=False,
                                   message_ids=(31,), uncertain_nonce=None)
        for hidden in (False, True):
            with self.subTest(hidden=hidden):
                self.context.state.proposed_changes[:] = [replace(self.action, details_hidden=hidden)]
                turns = []
                conversation = SimpleNamespace(append=turns.append, turns=turns, working=None)
                response = preview_text(self.context.state.proposed_changes)
                await AgentTurnMixin._record_turn(
                    recorder, self.message, member, "Change target", response, "",
                    self.context, delivery, conversation,
                )
                self.assertEqual(turns[0].source_channels,
                                 frozenset({10} if hidden else {10, 20}))
                self.assertEqual(turns[0].required_access,
                                 frozenset() if hidden else frozenset({ACCESS_LEAD}))
                self.assertEqual(SECRET in turns[0].text, not hidden)
                self.assertEqual(self.context.state.source_channels, {10})
                self.assertEqual(self.context.state.required_access, set())

    async def test_hidden_preview_still_confirms_and_details_open_ephemerally(self):
        delivery = AgentDeliveryMixin()
        delivery.bot = self.context.bot
        delivery.action_runner = SimpleNamespace(submit=AsyncMock())
        await delivery.send_response(
            self.message, preview_text(self.context.state.proposed_changes), None, context=self.context,
        )
        public = self.message.reply.await_args.args[0]
        self.assertNotIn(SECRET, public)
        self.assertIn("Change target", public)
        self.assertIn(ACTION_PREVIEW_DETAILS_HIDDEN, public)
        view = self.message.reply.await_args.kwargs["view"]
        self.assertIsInstance(view, ConfirmationView)
        self.assertIn(ACTION_PREVIEW_DETAILS_BUTTON, [item.label for item in view.children])
        interaction = self.interaction()
        await view.show_details(interaction)
        self.assertEqual(interaction.response.send_message.await_args.args[0], SECRET)
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        await view.confirm(self.interaction())
        delivery.action_runner.submit.assert_awaited_once()
        self.action.run.assert_not_awaited()

    async def test_details_button_checks_owner_source_access_and_expiration(self):
        await prepare_preview(self.context)
        view = ConfirmationView(2, tuple(self.context.state.proposed_changes), self.context)
        interaction = self.interaction(self.other)
        await view.show_details(interaction)
        self.assertNotIn(SECRET, str(interaction.response.send_message.await_args))
        self.source.viewers.remove(2)
        interaction = self.interaction()
        await view.show_details(interaction)
        self.assertNotIn(SECRET, str(interaction.response.send_message.await_args))
        self.source.viewers.add(2)
        interaction = self.interaction()
        await view.show_details(interaction)
        self.assertNotIn(SECRET, str(interaction.response.send_message.await_args))
        await view.on_timeout()
        self.assertTrue(all(item.disabled for item in view.children))

    async def test_preparation_provenance_is_attached_without_widening_answer_state(self):
        self.context.state.proposed_changes.clear()

        async def prepare(context, arguments):
            context.state.source_channels.add(20)
            context.state.required_access.add(ACCESS_LEAD)
            context.state.proposed_changes.append(self.action)
            return {"status": "confirmation_required"}

        result = await AgentService.execute_tool(
            name="synthetic_change", handler=prepare, arguments={}, context=self.context,
            capability_scope={"required_access": [ACCESS_LEAD]}, action_class=ActionClass.CHANGE,
        )
        self.assertNotIn(SECRET, result)
        self.assertEqual(self.context.state.source_channels, {10})
        self.assertEqual(self.context.state.required_access, set())
        proposal = self.context.state.proposed_changes[0]
        self.assertEqual(proposal.preview.detail_sources, frozenset({20}))
        self.assertEqual(proposal.preview.detail_access, frozenset({ACCESS_LEAD}))

    def test_unfinished_report_contains_labels_and_no_preview_text(self):
        report = AgentActionRunner._report({"steps": [{
            "action_label": "Synthetic change", "action_class": "change",
            "status": "queued", "preview_json": '["Synthetic private content"]',
        }]})
        self.assertIn("Synthetic change", report)
        self.assertNotIn(SECRET, report)

    async def test_provenance_without_explicit_details_hides_all_preview_lines(self):
        self.context.state.proposed_changes[0] = replace(
            self.action, preview=replace(self.action.preview, details=(), lines=(SECRET,)),
        )
        await prepare_preview(self.context)
        preview = preview_text(self.context.state.proposed_changes)
        self.assertNotIn(SECRET, preview)
        self.assertIn("Synthetic change", preview)
        self.assertIn(ACTION_PREVIEW_DETAILS_HIDDEN, preview)

    async def test_hidden_change_result_is_delivered_behind_the_private_result_button(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repository = AgentActionRepository(Path(directory.name) / "actions.sqlite3")
        progress = SimpleNamespace(id=40, edit=AsyncMock())
        self.destination.send = AsyncMock(return_value=progress)
        runner = AgentActionRunner(
            bot=SimpleNamespace(get_guild=lambda _: self.guild), repository=repository, guild_id=1,
        )
        await runner.recover()
        await prepare_preview(self.context)
        identifier = await runner.submit(
            self.context, tuple(self.context.state.proposed_changes), confirmer_id=2,
        )
        result = await runner.wait_run(identifier)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn(SECRET, str(self.destination.send.await_args_list))
        self.assertNotIn(SECRET, progress.edit.await_args.kwargs["content"])
        private_view = progress.edit.await_args.kwargs["view"]
        self.assertIsInstance(private_view, PrivateResultView)
        self.assertIn(SECRET, private_view.parts)
