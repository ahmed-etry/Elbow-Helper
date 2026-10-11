"""Recipients can durably opt out of only their repeating reminder DMs."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from features.agent.scheduled import test_agent_scheduled_runner as fixtures
from elbow_helper.features.agent.actions.store import AgentActionRepository
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.scheduled.dm_controls import ReminderStopButton, reminder_stop_view
from elbow_helper.features.agent.scheduled.tools import list_standing


class ReminderDMControlTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ScheduledRunnerTests.asyncSetUp
    _tick = fixtures.ScheduledRunnerTests._tick

    def configure(self):
        self.members = {identifier: SimpleNamespace(
            id=identifier, display_name=f"Synthetic {identifier}", name=f"synthetic.{identifier}",
            mention=f"<@{identifier}>", bot=False, send=AsyncMock(), guild=self.member.guild,
        ) for identifier in (2, 3, 4)}
        self.member = self.members[2]
        self.access.return_value = self.member
        self.member.guild.get_member = self.members.get
        self.runner.context_factory = lambda message, member: SimpleNamespace(
            source_message=message, member=member, guild=member.guild, state=AgentTurnState(),
        )
        for name, replacement in (
            ("has_access_requirements", Mock(return_value=True)),
            ("asyncio.sleep", AsyncMock()),
        ):
            handle = patch(
                "elbow_helper.features.agent.discord_actions.direct_messages." + name, replacement,
            )
            handle.start()
            self.addCleanup(handle.stop)

    def save(self, *, recipients=(2, 3, 4), kind="interval"):
        schedule = ({"kind": "once", "at_utc": "2020-01-01T00:00:00Z"} if kind == "once"
                    else {"kind": "interval", "anchor_utc": "2099-01-01T00:00:00Z",
                          "seconds": 60, "runs": 3})
        return self.repository.create_standing(
            kind="reminder", guild_id=1, requester_id=2, destination_channel_id=3,
            rule={"kind": "reminder", "text": "Synthetic reminder",
                  "dm_member_ids": list(recipients),
                  "schedule": schedule}, next_at=1,
        )

    def interaction(self, member_id, repository=None):
        return SimpleNamespace(
            user=SimpleNamespace(id=member_id), response=SimpleNamespace(
                defer=AsyncMock(), send_message=AsyncMock(),
            ), followup=SimpleNamespace(send=AsyncMock()),
            client=SimpleNamespace(get_cog=lambda _: SimpleNamespace(
                action_repository=repository or self.repository,
            )),
        )

    async def test_only_other_members_repeating_dms_have_buttons(self):
        self.configure()
        identity = self.save()
        await self._tick()
        self.assertNotIn("view", self.members[2].send.await_args.kwargs)
        for identifier in (3, 4):
            view = self.members[identifier].send.await_args.kwargs["view"]
            self.assertTrue(view.is_persistent())
            self.assertEqual(view.children[0].item.label, "Stop these messages")
            self.assertEqual(view.children[0].identifier, identity)
        for member in self.members.values():
            member.send.reset_mock()
        self.save(kind="once")
        await self._tick()
        for member in self.members.values():
            self.assertNotIn("view", member.send.await_args.kwargs)

    async def test_stop_removes_only_the_recipient_and_the_askers_list_updates(self):
        self.configure()
        identity = self.save()
        unrelated = self.save(recipients=(3,))
        button = reminder_stop_view(self.repository, identity, 3).children[0]
        press = self.interaction(3)
        await button.callback(press)
        press.followup.send.assert_awaited_once_with(
            "You won't get these messages anymore.", ephemeral=True,
        )
        row = self.repository.standing(kind="reminder", identifier=identity)
        self.assertEqual(row["rule"]["dm_member_ids"], [2, 4])
        self.assertEqual(row["status"], "active")
        self.assertEqual(self.repository.standing(kind="reminder", identifier=unrelated)
                         ["rule"]["dm_member_ids"], [3])
        context = SimpleNamespace(action_repository=self.repository, member=self.member,
                                  state=AgentTurnState())
        listed = (await list_standing(context, {"kind": "reminder"}))["rules"]
        self.assertEqual(next(row for row in listed if row["id"] == identity)["dm_member_ids"],
                         [2, 4])

    async def test_restart_rebuilds_the_button_and_repeated_presses_are_safe(self):
        self.configure()
        identity = self.save(recipients=(3,))
        original = reminder_stop_view(self.repository, identity, 3).children[0]
        restarted = AgentActionRepository(self.repository.path)
        press = self.interaction(3, restarted)
        match = original.template.fullmatch(original.custom_id)
        button = await ReminderStopButton.from_custom_id(press, original.item, match)
        await button.callback(press)
        await button.callback(press)
        row = restarted.standing(kind="reminder", identifier=identity)
        self.assertEqual(row["rule"]["dm_member_ids"], [])
        self.assertEqual(row["status"], "completed")
        self.assertEqual(press.followup.send.await_count, 2)

    async def test_other_members_cannot_press_the_recipients_button(self):
        self.configure()
        identity = self.save()
        press = self.interaction(4)
        await reminder_stop_view(self.repository, identity, 3).children[0].callback(press)
        self.assertEqual(self.repository.standing(kind="reminder", identifier=identity)
                         ["rule"]["dm_member_ids"], [2, 3, 4])
        press.followup.send.assert_not_awaited()

    async def test_stopping_during_pacing_prevents_delivery_and_survives_finish(self):
        self.configure()
        identity = self.save()

        async def stop_during_delay(_):
            self.repository.stop_reminder_recipient(identifier=identity, member_id=3)

        with patch("elbow_helper.features.agent.discord_actions.direct_messages.asyncio.sleep",
                   AsyncMock(side_effect=stop_during_delay)):
            await self._tick()
        self.members[3].send.assert_not_awaited()
        self.members[4].send.assert_awaited_once()
        row = self.repository.standing(kind="reminder", identifier=identity)
        self.assertEqual(row["rule"]["dm_member_ids"], [2, 4])
        self.assertEqual(row["rule"]["runs_completed"], 1)

    async def test_last_recipient_can_stop_without_a_channel_post_or_reactivation(self):
        self.configure()
        identity = self.save(recipients=(3,))

        async def stop_while_sending(*args, **kwargs):
            self.repository.stop_reminder_recipient(identifier=identity, member_id=3)

        self.members[3].send.side_effect = stop_while_sending
        await self._tick()
        row = self.repository.standing(kind="reminder", identifier=identity)
        self.assertEqual(row["status"], "completed")
        self.assertIsNone(row["next_run_at"])
        self.channel.send.assert_not_awaited()
        await self._tick()
        self.members[3].send.assert_awaited_once()

    async def test_start_registers_the_persistent_dynamic_handler(self):
        self.bot.add_dynamic_items = Mock()
        self.runner._main = AsyncMock()
        self.runner.start()
        await asyncio.gather(*tuple(self.runner._tasks))
        self.bot.add_dynamic_items.assert_called_once_with(ReminderStopButton)
