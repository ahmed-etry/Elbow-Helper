"""Reminders keep their delivery choice and run without the model."""
import unittest
from types import SimpleNamespace

from elbow_helper.features.agent.models import AgentRequestContext
from unittest.mock import AsyncMock, patch

from features.agent.scheduled import test_agent_scheduled_runner as fixtures
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.scheduled.tools import prepare_save
from elbow_helper.features.agent.scheduled.requests import run_saved_request, StandingDMUnavailable
from elbow_helper.features.agent.scheduled.watchers import comparison_data
from elbow_helper.features.agent.wording import ACTION_STANDING_DM_PAUSED
from elbow_helper.features.agent.actions.contracts import ActionRefused


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ScheduledRunnerTests.asyncSetUp
    _tick = fixtures.ScheduledRunnerTests._tick

    async def test_dm_reminders_use_the_dm_text_limit(self):
        self.member.bot = False
        self.member.display_name = "Asker"
        context = SimpleNamespace(
            action_repository=self.repository, member=self.member,
            guild=self.member.guild, source_message=SimpleNamespace(channel=self.channel),
            state=AgentTurnState(),
        )
        with self.assertRaises(ActionRefused):
            await prepare_save(context, {
                "kind": "reminder", "text": "x" * 4001,
                "dm_member_ids": [2],
                "schedule": {"kind": "once", "at_utc": "2099-01-01T00:00:00Z"},
            }, registry_factory=lambda: {})
        self.assertEqual(context.state.proposed_changes, [])

    async def test_preview_save_and_deliver_reminders(self):
        self.member.bot = False
        self.member.display_name = "Asker"
        self.member.send = AsyncMock()
        self.runner.context_factory = lambda message, member: SimpleNamespace(
            source_message=message, member=member, guild=member.guild, state=AgentTurnState(),
        )
        self.channel.mention = "<#3>"
        context = SimpleNamespace(
            action_repository=self.repository, member=self.member,
            guild=self.member.guild, source_message=SimpleNamespace(channel=self.channel),
            state=AgentTurnState(),
        )
        seen = set()
        for use_dm in (False, True):
            values = {"kind": "reminder", "text": "Time for war", "schedule": {
                "kind": "once", "at_utc": "2099-01-01T00:00:00Z",
            }}
            if use_dm:
                values["dm_member_ids"] = [2]
            with (
                patch(
                    "elbow_helper.features.agent.scheduled.tools.resolve_channel",
                    AsyncMock(return_value=self.channel),
                ),
                patch("elbow_helper.features.agent.scheduled.tools.check_post_access"),
            ):
                await prepare_save(context, values, registry_factory=lambda: {})
                action = context.state.proposed_changes[-1]
                self.assertEqual(action.preview.summary, "Set reminder")
                self.assertIn("DM <@2>." if use_dm else "Post in <#3>.", action.preview.lines)
                self.assertTrue(await action.preview.recheck())
                await action.run()
            rows = self.repository.list_standing(requester_id=2, kind="reminder")
            row = next(row for row in rows if row["request_id"] not in seen)
            seen.add(row["request_id"])
            self.assertEqual(row["kind"], "reminder")
            self.assertEqual(self.repository.list_standing(requester_id=2, kind="request"), [])
            with self.repository.connect() as connection:
                connection.execute(
                    "UPDATE saved_requests SET next_run_at=1 WHERE request_id=?",
                    (row["request_id"],),
                )
                connection.commit()
            with (
                patch(
                    "elbow_helper.features.agent.scheduled.runner.post_mentions", return_value=None,
                ),
                patch(
                    "elbow_helper.features.agent.scheduled.runner.require_evidence_access",
                    AsyncMock(),
                ),
            ):
                await self._tick()
            self.assertEqual(
                self.repository.standing(kind="reminder", identifier=row["request_id"])["status"],
                "active",
            )
        self.channel.send.assert_awaited_with("Time for war", allowed_mentions=None)
        self.member.send.assert_awaited_once()
        self.run_request.assert_not_awaited()

    async def test_request_dm_delivery_and_pause_notice(self):
        dm = SimpleNamespace(id=9)
        self.member.create_dm = AsyncMock(return_value=dm)
        message = SimpleNamespace(channel=self.channel)
        from elbow_helper.features.agent.scheduled.requests import ScheduledMessage
        message = ScheduledMessage(self.member.guild, self.channel, self.member, "Question")
        context = AgentRequestContext(
            source_message=message, member=self.member, guild=self.member.guild,
            bot=self.bot, account_links=None, clan_health=None, message_search=None,
            state=AgentTurnState(),
        )
        delivery = AsyncMock()
        with patch(
            "elbow_helper.features.agent.scheduled.requests.can_show_in_dm",
            AsyncMock(return_value=True),
        ):
            await run_saved_request(
                context, {"request": "Question", "deliver_to": "dm"},
                service=SimpleNamespace(answer=AsyncMock(return_value="Answer")),
                action_runner=SimpleNamespace(), delivery=delivery,
            )
        self.assertIs(delivery.await_args.args[0].channel, dm)
        identifier = self.repository.create_standing(
            kind="request", guild_id=1, requester_id=2, destination_channel_id=3,
            rule={"request": "Question", "deliver_to": "dm", "schedule": {
                "kind": "once", "at_utc": "2026-01-01T00:00:00Z",
            }}, next_at=1,
        )
        self.run_request.side_effect = StandingDMUnavailable()
        await self._tick()
        self.assertEqual(
            self.repository.standing(kind="request", identifier=identifier)["status"], "paused",
        )
        self.assertIn(
            ACTION_STANDING_DM_PAUSED.format(kind="Saved request"),
            self.channel.send.await_args.args[0],
        )

    def test_watcher_ignores_observation_and_flags(self):
        self.assertEqual(
            comparison_data({"rows": [1], "observed_at": "a", "flags": ["x"]}),
            comparison_data({"rows": [1], "observed_at": "b", "flags": []}),
        )
