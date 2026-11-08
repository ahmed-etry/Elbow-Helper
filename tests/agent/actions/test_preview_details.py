"""Hidden change details remain private while confirmation stays available."""

from types import SimpleNamespace
from dataclasses import replace
from pathlib import Path
from tests.temp_directory import TemporaryDirectory
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import CORE, LEAD
from elbow_helper.agent.safety.access import ACCESS_LEAD
from elbow_helper.agent.actions.contracts import (
    ActionClass, ActionRefused, ChangePreview, PreparedAction,
)
from elbow_helper.agent.actions.details import prepare_preview
from elbow_helper.agent.actions.outcomes import ActionOutcome
from elbow_helper.agent.actions.preview import ConfirmationView, preview_text
from elbow_helper.agent.actions.store import AgentActionRepository
from elbow_helper.agent.conversation.delivery import AgentDeliveryMixin
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.models import AgentRequestContext
from elbow_helper.agent.engine.models import RegisteredAgentTool
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.agent.scheduled.tools import prepare_manage, prepare_save
from elbow_helper.agent.conversation.wording import ACTION_PREVIEW_DETAILS_HIDDEN
from tests.agent.safety.test_agent_audiences import Channel, Member, Role


SECRET = "Synthetic private content"


class PreviewDetailTests(unittest.IsolatedAsyncioTestCase):

    async def test_preview_pings_no_one_the_request_mentioned(self):
        delivery = AgentDeliveryMixin()
        delivery.bot = self.context.bot
        delivery.action_runner = object()
        mentioned = Member(5, ())
        self.message.mentions = (mentioned,)
        referenced = SimpleNamespace(author=self.other)
        await delivery.send_response(self.message, "Synthetic answer", referenced)
        self.assertEqual(self.message.reply.await_args.kwargs["allowed_mentions"].users,
                         [mentioned, self.other])
        await delivery.send_response(self.message, preview_text([self.action]), referenced,
                                     context=self.context)
        self.assertEqual(self.message.reply.await_args.kwargs["allowed_mentions"].users, [])


    async def test_watcher_save_checks_the_destination_audience(self):
        tool = RegisteredAgentTool(AgentToolDefinition("synthetic_read", "Synthetic", {
            "type": "object", "properties": {"channel_id": {"type": "integer"}},
            "required": ["channel_id"],
        }), AsyncMock(), contract=CapabilityContract(
            (("channel_id", "discord_channel"),), channel_fields=("channel_id",),
            required_access=frozenset({ACCESS_LEAD}),
        ))
        with TemporaryDirectory() as directory:
            repository = AgentActionRepository(Path(directory) / "actions.sqlite3")
            self.context = replace(self.context, action_repository=repository)
            self.context.state.proposed_changes.clear()
            values = {"kind": "watcher", "request": "Synthetic", "timezone": "UTC",
                      "schedule": {"kind": "interval", "seconds": 1800,
                                   "anchor_utc": "2099-01-01T00:00:00Z"},
                      "destination_channel_id": 10, "condition": "Synthetic condition",
                      "reads": [{"capability": "synthetic_read", "arguments": {"channel_id": 20}}]}
            run_at = datetime.now(timezone.utc) + timedelta(hours=1)
            with (patch("elbow_helper.agent.scheduled.tools.resolve_channel",
                        new=AsyncMock(return_value=self.destination)),
                  patch("elbow_helper.agent.scheduled.tools.check_post_access"),
                  patch("elbow_helper.agent.scheduled.tools._schedule", return_value=(run_at,))):
                with self.assertRaises(ActionRefused):
                    await prepare_save(self.context, values, registry_factory=lambda: {"synthetic_read": tool})
                self.assertEqual(self.context.state.proposed_changes, [])
                self.destination.viewers.remove(3)
                await prepare_save(self.context, values, registry_factory=lambda: {"synthetic_read": tool})
                self.assertEqual(len(self.context.state.proposed_changes), 1)
                self.destination.viewers.add(3)
                self.assertFalse(await self.context.state.proposed_changes[0].preview.recheck())

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
            source_message=self.message, account_links=None, message_search=None,
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
            with (patch("elbow_helper.agent.scheduled.tools.resolve_channel",
                        new=AsyncMock(return_value=self.destination)),
                  patch("elbow_helper.agent.scheduled.tools.check_post_access"),
                  patch("elbow_helper.agent.scheduled.tools._schedule",
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
        self.context = replace(self.context, private_pass=True)
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

    async def test_model_cannot_reuse_out_of_scope_titles_in_preview(self):
        from elbow_helper.agent.safety.access import AgentAccessLost
        self.context.state.proposed_changes.clear()
        self.context.state.knowledge_title_access = {"recruiter"}
        prepare = AsyncMock()
        with self.assertRaises(AgentAccessLost):
            await AgentService.execute_tool(name="synthetic_change", handler=prepare,
                                           arguments={}, context=self.context,
                                           action_class=ActionClass.CHANGE)
        prepare.assert_not_awaited()
        self.assertFalse(self.context.state.proposed_changes)


    async def test_provenance_without_explicit_details_hides_all_preview_lines(self):
        self.context.state.proposed_changes[0] = replace(
            self.action, preview=replace(self.action.preview, details=(), lines=(SECRET,)),
        )
        await prepare_preview(self.context)
        preview = preview_text(self.context.state.proposed_changes)
        self.assertNotIn(SECRET, preview)
        self.assertIn("Synthetic change", preview)
        self.assertIn(ACTION_PREVIEW_DETAILS_HIDDEN, preview)
