"""Recruitment state and history stay together across failures and restarts."""

import asyncio
from pathlib import Path
import sqlite3
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.configuration.roles import RECRUITERS
from elbow_helper.data.events import EventsStore, load_state, record, save_state
from elbow_helper.features.recruitment import trials as trial_module
from elbow_helper.features.recruitment.commands import RecruitmentCommandMixin
from elbow_helper.features.recruitment.state import (
    RecruitmentState, default_state, decision_event, trial_event, trial_started_at,
    parse_ticket_activity, serialize_ticket_activity,
)
from elbow_helper.features.recruitment.trials import TrialMixin

START = "2020-01-01T00:00:00+00:00"
START_US = 1577836800 * 1_000_000


class Owner(TrialMixin, RecruitmentCommandMixin):
    pass


def saved_work():
    return {
        "trials": {
            "200": {"start": START, "days": 7, "applicant_id": 42,
                    "tracking_msg_id": 500, "tracking_channel_id": 600},
            "201": {"start": "2999-01-01T00:00:00+00:00", "days": 3, "applicant_id": 43,
                    "tracking_msg_id": 501, "tracking_channel_id": 600},
            "202": {"days": 7, "applicant_id": 44},
        },
        "reminders": {
            "300": {"channel_id": 800, "message_id": 700, "applicant_id": 45, "created_at": 1700000000,
                    "resolved_at": None, "resolved_by": None},
            "301": {"channel_id": 800, "message_id": 701, "applicant_id": 46, "created_at": 1700000000,
                    "resolved_at": 1700000100, "resolved_by": 7},
        },
        "ticket_activity": {
            "400": {"last_reminder_at": "2026-10-21T17:00:00+00:00", "message_id": None,
                    "message_channel_id": None, "last_applicant_message_id": 9},
            "401": "2026-10-20T17:00:00+00:00",
            "402": {"last_reminder_at": None},
        },
        "ai_messages": {"900": {"channel_id": 800, "created_at": "2026-10-21T17:00:00+00:00"}},
    }


class RecruitmentHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = self.root / "events.sqlite3"
        self.recruiter = SimpleNamespace(id=7, roles=[SimpleNamespace(id=next(iter(RECRUITERS)))])

    async def seed(self, data):
        payload = {**default_state(), **data}
        payload["ticket_activity"] = serialize_ticket_activity(parse_ticket_activity(payload["ticket_activity"]))
        for info in payload["reminders"].values():
            info["_case_started_at"] = info["created_at"] * 1_000_000
        def write(connection):
            save_state(connection, "recruitment", payload)
            for ticket in ("200", "201"):
                if ticket in payload["trials"]:
                    info = payload["trials"][ticket]
                    record(connection, trial_event("trial_started", ticket, info, via="import",
                           at=trial_started_at(info), detail={"days": info["days"]}))
        await EventsStore(self.db).call(write)

    async def new_state(self):
        state = RecruitmentState(EventsStore(self.db))
        await state.startup()
        return state

    async def new_owner(self):
        owner = Owner()
        owner.recruitment_state = await self.new_state()
        owner.logger = MagicMock()
        owner._trial_lock, owner._reminder_lock = asyncio.Lock(), asyncio.Lock()
        self.reminder_message = SimpleNamespace(id=710, delete=AsyncMock(), edit=AsyncMock())
        self.rec_room = SimpleNamespace(id=800, send=AsyncMock(return_value=self.reminder_message),
                                        fetch_message=AsyncMock(return_value=self.reminder_message))
        self.tracking = SimpleNamespace(id=600, send=AsyncMock(return_value=SimpleNamespace(id=510)))
        self.ticket = SimpleNamespace(id=200, name="ticket-applicant", mention="<#200>",
                                      guild=SimpleNamespace(id=1), send=AsyncMock(), edit=AsyncMock())
        channels = {trial_module.REC_ROOM: self.rec_room, trial_module.TRIAL_LIST: self.tracking, 200: self.ticket}
        owner.bot = SimpleNamespace(get_channel=channels.get, get_guild=lambda _: None)
        owner._delete_tracking_message = AsyncMock(return_value=True)
        owner._build_trial_tracking_embed = MagicMock(return_value=None)
        owner._build_trial_reminder_embed = MagicMock(return_value=None)
        owner._rename_trial_ticket = AsyncMock(return_value=True)
        return owner


    async def test_trial_case_runs_from_start_through_expiry_to_follow_up(self):
        owner = await self.new_owner()
        result = await owner.start_trial_for_accept(self.ticket, 7, 42, actor_id=7, via="button")
        self.assertTrue(result.started)
        trial = owner.recruitment_state.trials()["200"]
        self.assertEqual((await owner.recruitment_state.events.call(load_state, "recruitment"))["trials"], {"200": trial})
        expired = {**trial, "start": START}
        await owner.recruitment_state.change(lambda state: state["trials"].update({"200": expired}) or ())
        await TrialMixin.check_expired_trials.coro(owner)
        self.assertEqual(owner.recruitment_state.trials(), {})
        reminder = owner.recruitment_state.reminders()["200"]
        self.assertEqual(reminder["_case_started_at"], START_US)
        with patch.object(trial_module, "can_rename", return_value=True):
            ended = await owner.complete_trial_end(200, 42, self.recruiter, allow_missing=True,
                                                   resolve_reminder=True, via="agent")
        self.assertTrue(ended.ended)
        rows = await owner.recruitment_state.events.read(workflow="recruitment")
        self.assertEqual([row["kind"] for row in rows],
                         ["trial_started", "trial_ended", "trial_followup_resolved"])
        self.assertEqual(rows[0]["case_key"], f"200:{trial_started_at(trial)}")
        self.assertEqual({row["case_key"] for row in rows[1:]}, {f"200:{START_US}"})
        self.assertEqual((rows[0]["actor_discord_id"], rows[0]["via"]), (7, "button"))
        self.assertEqual((rows[1]["actor_kind"], rows[1]["detail"]), ("automatic", {"how": "expired"}))
        self.assertEqual((rows[2]["actor_discord_id"], rows[2]["actor_kind"]), (7, "agent"))
        self.assertEqual((await owner.recruitment_state.events.call(load_state, "recruitment"))["reminders"]["200"]["resolved_by"], 7)

    async def test_restart_mid_expiry_keeps_the_reminder_and_finishes_once(self):
        await self.seed({"trials": {"200": saved_work()["trials"]["200"]}})
        owner = await self.new_owner()
        owner._delete_tracking_message.return_value = False
        await TrialMixin.check_expired_trials.coro(owner)
        self.assertIn("200", owner.recruitment_state.trials())
        self.assertIn("200", owner.recruitment_state.reminders())
        owner = await self.new_owner()
        await TrialMixin.check_expired_trials.coro(owner)
        self.rec_room.send.assert_not_awaited()
        self.assertEqual(owner.recruitment_state.trials(), {})
        kinds = [row["kind"] for row in await owner.recruitment_state.events.read()]
        self.assertEqual(kinds, ["trial_started", "trial_ended"])

    async def test_failed_write_keeps_the_trial_open_with_no_history(self):
        await self.seed({"trials": {"200": saved_work()["trials"]["200"]}})
        owner = await self.new_owner()
        events = owner.recruitment_state.events
        await events.call(lambda c: c.execute(
            "CREATE TRIGGER fail_end BEFORE INSERT ON workflow_events WHEN NEW.kind='trial_ended' "
            "BEGIN SELECT RAISE(ABORT,'failed'); END"))
        with self.assertRaises(sqlite3.IntegrityError):
            await owner.complete_trial_end(200, 42, self.recruiter)
        self.assertIn("200", owner.recruitment_state.trials())
        self.assertIn("200", (await owner.recruitment_state.events.call(load_state, "recruitment"))["trials"])
        await events.call(lambda c: c.execute("DROP TRIGGER fail_end"))
        owner = await self.new_owner()
        self.assertIn("200", owner.recruitment_state.trials())
        self.assertTrue((await owner.complete_trial_end(200, 42, self.recruiter)).ended)
        row = (await owner.recruitment_state.events.read(kind="trial_ended"))[0]
        self.assertEqual((row["detail"], row["via"], row["actor_kind"]), ({"how": "early"}, "button", "member"))

    async def test_applicant_leaving_ends_their_trial_automatically(self):
        await self.seed({"trials": saved_work()["trials"]})
        owner = await self.new_owner()
        await owner.on_member_remove(SimpleNamespace(id=42))
        self.assertNotIn("200", owner.recruitment_state.trials())
        row, = await owner.recruitment_state.events.read(kind="trial_ended")
        self.assertEqual((row["subject_discord_id"], row["actor_kind"], row["detail"]),
                         (42, "automatic", {"how": "applicant_left"}))

    async def test_two_resolutions_of_one_reminder_record_one_event(self):
        await self.seed({"reminders": {"300": saved_work()["reminders"]["300"]}})
        owner = await self.new_owner()
        await asyncio.gather(*(owner._mark_trial_reminder_resolved(
            ticket_channel_id=300, applicant_id=45, resolver_id=7) for _ in range(2)))
        self.assertEqual(len(await owner.recruitment_state.events.read(kind="trial_followup_resolved")), 1)

    async def test_decisions_are_history_and_a_failed_save_never_undoes_the_message(self):
        owner = await self.new_owner()
        prepared = {"applicant": SimpleNamespace(id=42, display_name="Applicant"),
                    "channel": self.ticket, "message": "Declined.", "rename_candidate": None}
        await owner.post_decline(prepared, actor_id=7)
        await owner.recruitment_state.events.call(lambda c: c.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON workflow_events BEGIN SELECT RAISE(ABORT,'failed'); END"))
        text = await owner.post_checkup({"channel": self.ticket, "message": "Hello", "confirmation": "Sent.",
                                         "applicant_id": 42}, actor_id=7)
        self.assertEqual(text, "Sent.")
        self.assertEqual(self.ticket.send.await_count, 2)
        owner.logger.exception.assert_called_once()
        row, = await owner.recruitment_state.events.read()
        self.assertEqual((row["kind"], row["actor_discord_id"], row["actor_kind"], row["via"], row["detail"]),
                         ("declined", 7, "member", "command", {}))
        self.assertNotEqual(row["case_key"], f"200:{START_US}")


    async def test_delete_member_removes_their_open_trial_reminder_history_and_attribution(self):
        await self.seed({"trials": saved_work()["trials"], "reminders": saved_work()["reminders"]})
        state = await self.new_state()
        await state.record_event(decision_event("declined", 300, 45, actor_id=8, via="command"))
        await state.events.delete_member(42)
        await state.events.delete_member(45)
        await state.events.delete_member(7)
        self.assertNotIn("200", state.trials())
        self.assertEqual(set(state.reminders()), {"301"})
        self.assertIsNone(state.reminders()["301"]["resolved_by"])
        self.assertEqual({row["subject_discord_id"] for row in await state.events.read()}, {43})
        self.assertEqual((await state.events.call(load_state, "recruitment"))["reminders"], state.reminders())


if __name__ == "__main__":
    unittest.main()
