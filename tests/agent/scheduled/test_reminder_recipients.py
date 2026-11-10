"""Confirmed reminders retain recipients, permissions and legacy delivery."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from tests.agent.scheduled import test_agent_scheduled_runner as fixtures
from elbow_helper.agent.actions.contracts import ActionRefused
from elbow_helper.agent.actions.store import AgentActionRepository
from elbow_helper.agent.discord_actions.direct_messages import DirectMessageAccessDenied
from elbow_helper.agent.engine.models import AgentTurnState
from elbow_helper.agent.scheduled.tools import list_standing, prepare_save, standing_tools


class ReminderRecipientTests(unittest.IsolatedAsyncioTestCase):
    _tick = fixtures.ScheduledRunnerTests._tick

    async def asyncSetUp(self):
        await fixtures.ScheduledRunnerTests.asyncSetUp(self)
        guild = self.member.guild
        self.members = {identifier: SimpleNamespace(
            id=identifier, display_name=f"Member {identifier}", mention=f"<@{identifier}>",
            bot=False, guild=guild, send=AsyncMock(),
        ) for identifier in (2, 4, 5)}
        self.member = self.members[2]
        self.access.return_value = self.member
        guild.get_member = self.members.get
        self.channel.mention = "<#3>"
        self.context = SimpleNamespace(
            guild=guild, member=self.member, action_repository=self.repository,
            source_message=SimpleNamespace(channel=self.channel, raw_mentions=(4, 5)),
            state=AgentTurnState(),
        )
        self.runner.context_factory = lambda message, member: SimpleNamespace(
            guild=guild, member=member, source_message=message, state=AgentTurnState(),
        )
        for name, replacement in (
            ("elbow_helper.agent.scheduled.tools.resolve_channel", AsyncMock(return_value=self.channel)),
            ("elbow_helper.agent.scheduled.tools.check_post_access", None),
            ("elbow_helper.agent.scheduled.runner.require_evidence_access", AsyncMock()),
            ("elbow_helper.agent.discord_actions.direct_messages.asyncio.sleep", AsyncMock()),
        ):
            handle = patch(name, replacement) if replacement is not None else patch(name)
            handle.start()
            self.addCleanup(handle.stop)

    async def prepare(self, **changes):
        values = {
            "kind": "reminder", "text": "End the trial", "member_ids": [4, 5],
            "schedule": {"kind": "once", "at_utc": "2099-01-01T00:00:00Z"},
            **changes,
        }
        await prepare_save(self.context, values, registry_factory=lambda: {})
        return self.context.state.proposed_changes[-1]

    async def save_due(self, **changes):
        action = await self.prepare(**changes)
        self.assertTrue(await action.preview.recheck())
        await action.run()
        row = self.repository.list_standing(requester_id=2, kind="reminder")[0]
        with self.repository.connect() as connection:
            connection.execute("UPDATE saved_requests SET next_run_at=1 WHERE request_id=?",
                               (row["request_id"],))
            connection.commit()
        return row["request_id"]

    def status(self, identifier):
        return self.repository.standing(kind="reminder", identifier=identifier)["status"]

    async def test_channel_reminder_previews_saves_and_pings_all_members_after_restart(self):
        identity = await self.save_due()
        self.assertIn("Post in <#3> and ping <@4>, <@5>.",
                      self.context.state.proposed_changes[-1].preview.lines)
        self.runner.repository = AgentActionRepository(self.repository.path)
        listed = (await list_standing(self.context, {"kind": "reminder"}))["rules"][0]
        self.assertEqual(listed["member_ids"], [4, 5])
        await self._tick()
        self.channel.send.assert_awaited_once()
        sent = self.channel.send.await_args
        self.assertEqual(sent.args[0], "<@4> <@5>\nEnd the trial")
        mentions = sent.kwargs["allowed_mentions"]
        self.assertTrue(mentions.users)
        self.assertFalse(mentions.everyone)
        self.assertEqual(mentions.roles, [])
        self.members[4].send.assert_not_awaited()
        self.assertEqual(self.status(identity), "active")
        self.run_request.assert_not_awaited()

    async def test_dm_delivery_uses_selected_members_and_existing_controls(self):
        with patch("elbow_helper.agent.discord_actions.direct_messages.has_access_requirements",
                   return_value=True):
            identity = await self.save_due(deliver_to="dm", schedule={
                "kind": "interval", "anchor_utc": "2099-01-01T00:00:00Z",
                "seconds": 900, "runs": 2,
            })
            action = self.context.state.proposed_changes[-1]
            self.assertIn("DM <@4>, <@5>.", action.preview.lines)
            self.assertEqual(action.values["dm_member_ids"], [4, 5])
            self.assertNotIn("member_ids", action.values)
            await self._tick()
        for identifier in (4, 5):
            self.members[identifier].send.assert_awaited_once()
            sent = self.members[identifier].send.await_args
            self.assertIn("End the trial", sent.args[0])
            self.assertIn("view", sent.kwargs)
        self.member.send.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.status(identity), "active")

    async def test_self_dm_does_not_need_lead(self):
        with patch("elbow_helper.agent.discord_actions.direct_messages.has_access_requirements",
                   return_value=False):
            await self.save_due(deliver_to="dm", member_ids=[2])
            await self._tick()
        self.member.send.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_other_member_dms_still_need_lead_at_preview_confirm_and_delivery(self):
        path = "elbow_helper.agent.discord_actions.direct_messages.has_access_requirements"
        with patch(path, return_value=False):
            with self.assertRaises(DirectMessageAccessDenied) as error:
                await self.prepare(deliver_to="dm")
            self.assertEqual(str(error.exception), "DMs to other members need lead access.")
        with patch(path, return_value=True):
            identity = await self.save_due(deliver_to="dm")
        with patch(path, return_value=False):
            self.assertFalse(await self.context.state.proposed_changes[-1].preview.recheck())
            await self._tick()
        self.members[4].send.assert_not_awaited()
        self.members[5].send.assert_not_awaited()
        self.assertEqual(self.status(identity), "paused")

    async def test_channel_targets_need_the_same_provenance_as_posts(self):
        self.context.source_message.raw_mentions = ()
        with self.assertRaises(ActionRefused) as error:
            await self.prepare()
        self.assertIn("no target provenance", str(error.exception))
        self.assertEqual(self.context.state.proposed_changes, [])

    async def test_scheduled_pings_only_authorize_the_confirmed_recipient_list(self):
        self.context.source_message.raw_mentions = (2, 4, 5)
        identity = await self.save_due(text="End the trial <@2>")
        await self._tick()
        self.assertEqual(self.status(identity), "paused")
        self.assertNotIn("End the trial", self.channel.send.await_args.args[0])

    async def test_role_and_everyone_ping_validation_still_matches_posts(self):
        self.context.guild.get_role = lambda _: None
        for changes in ({"ping_role_ids": [9]}, {"ping_everyone": True}):
            with self.subTest(changes=changes):
                with self.assertRaises(ActionRefused):
                    await self.prepare(**changes)

    async def test_long_reminder_chunks_still_include_the_recipient_pings(self):
        await self.save_due(text="Synthetic reminder " * 300)
        await self._tick()
        sent = self.channel.send.await_args_list
        self.assertGreater(len(sent), 1)
        self.assertEqual(sent[0].args[0].splitlines()[0], "<@4> <@5>")
        self.assertTrue(all(len(call.args[0]) <= 2000 for call in sent))
        self.assertTrue(all(call.kwargs["allowed_mentions"].users for call in sent))

    async def test_invalid_or_missing_channel_recipients_are_refused(self):
        for identifiers in (None, [], [4, 4], [99], [True], ["4"], [-1], list(range(51))):
            with self.subTest(identifiers=identifiers):
                with self.assertRaises(ActionRefused):
                    await self.prepare(member_ids=identifiers)
        self.assertEqual(self.context.state.proposed_changes, [])

    async def test_channel_reminder_without_recipients_posts_unchanged(self):
        values = {"kind": "reminder", "text": "War starts soon",
                  "schedule": {"kind": "once", "at_utc": "2099-01-01T00:00:00Z"}}
        await prepare_save(self.context, values, registry_factory=lambda: {})
        self.assertIn("Post in <#3>.", self.context.state.proposed_changes[-1].preview.lines)

    async def test_different_dm_and_channel_recipient_lists_are_refused(self):
        for delivery in ("channel", "dm"):
            with self.assertRaises(ActionRefused):
                await self.prepare(deliver_to=delivery, dm_member_ids=[2])

    async def test_departed_recipient_invalidates_preview_and_pauses_delivery(self):
        identity = await self.save_due()
        self.members.pop(5)
        self.assertFalse(await self.context.state.proposed_changes[-1].preview.recheck())
        await self._tick()
        self.assertEqual(self.status(identity), "paused")
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertNotIn("End the trial", self.channel.send.await_args.args[0])

    async def test_lost_post_permission_pauses_without_sending_reminder(self):
        identity = await self.save_due()
        with patch("elbow_helper.agent.scheduled.runner.check_post_access",
                   side_effect=ActionRefused("Synthetic permission loss")):
            await self._tick()
        self.assertEqual(self.status(identity), "paused")
        self.assertNotIn("End the trial", self.channel.send.await_args.args[0])

    async def test_all_dm_deliveries_failing_pauses_the_reminder(self):
        with patch("elbow_helper.agent.discord_actions.direct_messages.has_access_requirements",
                   return_value=True):
            identity = await self.save_due(deliver_to="dm")
            for member in (self.members[4], self.members[5]):
                member.send.side_effect = discord.Forbidden(
                    SimpleNamespace(status=403, reason="Forbidden"),
                    {"code": 50007, "message": "Cannot send messages to this user"},
                )
            await self._tick()
        self.assertEqual(self.status(identity), "paused")

    async def test_old_channel_reminder_fires_with_unchanged_text_and_mentions(self):
        identity = self.repository.create_standing(
            kind="reminder", guild_id=1, requester_id=2, destination_channel_id=3,
            rule={"text": "Legacy reminder", "schedule": {
                "kind": "once", "at_utc": "2020-01-01T00:00:00Z",
            }}, next_at=1,
        )
        await self._tick()
        self.assertEqual(self.channel.send.await_args.args[0], "Legacy reminder")
        self.assertTrue(self.channel.send.await_args.kwargs["allowed_mentions"].users)
        self.assertEqual(self.status(identity), "completed")

    def test_member_targets_are_declared_in_schema_and_contract(self):
        tool = standing_tools(lambda: {})[0]
        self.assertIn("member_ids", tool.definition.parameters["properties"])
        self.assertIn(("member_ids", "discord_member_set"), tool.contract.entity_fields)
