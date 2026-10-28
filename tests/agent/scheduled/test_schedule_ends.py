"""Bounded recurring schedules stop, survive restart and expose all short runs."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent.scheduled import test_agent_scheduled_runner as fixtures
from elbow_helper.agent.actions.store import AgentActionRepository
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.agent.scheduled.time_rules import (
    next_occurrences, remaining_runs, schedule_preview,
)
from elbow_helper.agent.scheduled.tools import list_standing, prepare_save


START = datetime(2099, 1, 1, tzinfo=timezone.utc)
INTERVAL = {"kind": "interval", "anchor_utc": START.isoformat(), "seconds": 60}


class ScheduleEndTimeTests(unittest.TestCase):
    def test_all_repeat_kinds_accept_time_and_run_ends(self):
        rules = (INTERVAL, {"kind": "weekly", "days": [0], "time": "12:00",
                           "timezone": "Europe/Paris"},
                 {"kind": "monthly", "days": [31], "time": "12:00",
                  "timezone": "Europe/Paris"})
        for rule in rules:
            after = START - timedelta(seconds=1)
            times = next_occurrences(rule, after=after, count=3)
            bounded = {**rule, "end_utc": (times[1] + timedelta(seconds=1)).isoformat()}
            self.assertEqual(schedule_preview(bounded, after=after), times[:2])
            self.assertEqual(next_occurrences(bounded, after=times[1]), ())
            self.assertEqual(schedule_preview({**rule, "runs": 2}, after=after), times[:2])
            self.assertEqual(next_occurrences({**rule, "runs": 2}, after=after,
                                             runs_completed=2), ())
            self.assertEqual(remaining_runs(bounded, next_at=times[0].timestamp(), now=after), 2)

    def test_preview_lists_all_ten_runs_and_limits_longer_schedules(self):
        after = START - timedelta(seconds=1)
        self.assertEqual(len(schedule_preview({**INTERVAL, "runs": 10}, after=after)), 10)
        self.assertEqual(len(schedule_preview({**INTERVAL, "runs": 11}, after=after)), 3)
        end = START + timedelta(minutes=9)
        self.assertEqual(len(schedule_preview({**INTERVAL, "end_utc": end.isoformat()},
                                             after=after)), 10)

    def test_end_validation_is_specific(self):
        cases = (({"end_utc": START.isoformat()}, "The end must be after the first run."),
                 ({"runs": 0}, "Choose a positive number of runs."),
                 ({"runs": True}, "Choose a positive number of runs."),
                 ({"runs": 2, "end_utc": START.isoformat()},
                  "Choose an end time or a number of runs, not both."))
        for fields, message in cases:
            with self.assertRaises(ValueError) as caught:
                schedule_preview({**INTERVAL, **fields}, after=START - timedelta(seconds=1))
            self.assertEqual(str(caught.exception), message)
        with self.assertRaisesRegex(ValueError, "One-off schedules cannot have an end"):
            schedule_preview({"kind": "once", "at_utc": START.isoformat(), "runs": 1},
                             after=START - timedelta(seconds=1))


class ScheduleEndRunnerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ScheduledRunnerTests.asyncSetUp
    _tick = fixtures.ScheduledRunnerTests._tick

    async def test_run_counts_persist_and_complete_for_requests_watchers_and_reminders(self):
        self.runner.context_factory = lambda message, member: SimpleNamespace(
            source_message=message, member=member, guild=member.guild, state=AgentTurnState(),
        )
        for kind in ("request", "watcher", "reminder"):
            identity = self.repository.create_standing(
                kind=kind, guild_id=1, requester_id=2, destination_channel_id=3,
                rule={"request": "Synthetic", "text": "Synthetic", "schedule": {
                    **INTERVAL, "seconds": 900, "runs": 2,
                }}, next_at=1,
            )
            await self._tick()
            restarted = AgentActionRepository(self.repository.path)
            self.runner.repository = restarted
            row = restarted.standing(kind=kind, identifier=identity)
            self.assertEqual(row["rule"]["runs_completed"], 1)
            self.assertEqual(row["status"], "active")
            table, key, due = restarted._standing_columns(kind)
            with restarted.connect() as connection:
                connection.execute(f"UPDATE {table} SET {due}=1 WHERE {key}=?", (identity,))
                connection.commit()
            await self._tick()
            row = restarted.standing(kind=kind, identifier=identity)
            self.assertEqual(row["rule"]["runs_completed"], 2)
            self.assertEqual(row["status"], "completed")
            self.assertIsNone(row[due])
            await self._tick()

    async def test_expired_and_paused_schedules_complete_without_delivering(self):
        for status in ("active", "paused"):
            identity = self.repository.create_standing(
                kind="request", guild_id=1, requester_id=2, destination_channel_id=3,
                rule={"request": "Synthetic", "schedule": {
                    "kind": "interval", "anchor_utc": "2020-01-01T00:00:00Z",
                    "seconds": 60, "end_utc": "2020-01-01T00:10:00Z",
                }}, next_at=1,
            )
            if status == "paused":
                self.repository.set_standing_status(kind="request", identifier=identity,
                                                    requester_id=2, status=status)
            await self._tick()
            self.assertEqual(self.repository.standing(kind="request", identifier=identity)
                             ["status"], "completed")
        self.run_request.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_confirmation_and_list_show_bounds_and_all_short_run_times(self):
        self.channel.mention = "<#3>"
        context = SimpleNamespace(
            action_repository=self.repository, member=self.member, guild=self.member.guild,
            source_message=SimpleNamespace(channel=self.channel), state=AgentTurnState(),
        )
        for fields in ({"runs": 2}, {"end_utc": (START + timedelta(minutes=1)).isoformat()}):
            with patch("elbow_helper.agent.scheduled.tools.resolve_channel",
                       AsyncMock(return_value=self.channel)), patch(
                "elbow_helper.agent.scheduled.tools.check_post_access",
            ):
                await prepare_save(context, {
                    "kind": "reminder", "text": "Synthetic", "schedule": {**INTERVAL, **fields},
                }, registry_factory=lambda: {})
            action = context.state.proposed_changes.pop()
            self.assertIn(f"<t:{int(START.timestamp())}:f>", action.preview.lines[0])
            self.assertIn(f"<t:{int((START + timedelta(minutes=1)).timestamp())}:f>",
                          action.preview.lines[0])
            expected = ("Ends after 2 runs." if "runs" in fields else
                        f"Ends: <t:{int((START + timedelta(minutes=1)).timestamp())}:f>")
            self.assertIn(expected, action.preview.details)
            await action.run()
            identity = self.repository.list_standing(requester_id=self.member.id)[0]["request_id"]
            self.repository.set_standing_status(kind="reminder", identifier=identity,
                                                requester_id=self.member.id, status="paused")
        records = (await list_standing(context, {}))["rules"]
        self.assertEqual([row["runs_left"] for row in records], [2, 2])
        self.assertEqual({row["runs"] for row in records}, {2, None})
