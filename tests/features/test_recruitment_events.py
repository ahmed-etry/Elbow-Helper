"""Recruitment state lives with its history; the old files are a one-update mirror."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.configuration.roles import RECRUITERS
from elbow_helper.data.events import EventsStore, load_state, record
from elbow_helper.features.recruitment import trials as trial_module
from elbow_helper.features.recruitment.commands import RecruitmentCommandMixin
from elbow_helper.features.recruitment.queries import RecruitmentQueries
from elbow_helper.features.recruitment.state import (
    LEGACY_FILES, RecruitmentState, decision_event, trial_started_at,
)
from elbow_helper.features.recruitment.trials import TrialMixin

START = "2020-01-01T00:00:00+00:00"
START_US = 1577836800 * 1_000_000


class Owner(TrialMixin, RecruitmentCommandMixin):
    pass


def legacy():
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
        self.paths = {part: self.root / f"{part}.json" for part in LEGACY_FILES}
        self.recruiter = SimpleNamespace(id=7, roles=[SimpleNamespace(id=next(iter(RECRUITERS)))])

    def write_legacy(self, data=None):
        for part, payload in (data or legacy()).items():
            self.paths[part].write_text(json.dumps(payload), encoding="utf-8")

    async def new_state(self):
        state = RecruitmentState(EventsStore(self.db), self.paths)
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

    async def test_first_start_imports_open_work_once_and_a_restart_imports_nothing(self):
        self.write_legacy()
        state = await self.new_state()
        source = legacy()
        self.assertEqual(state.trials(), source["trials"])
        self.assertEqual(set(state.reminders()), {"300", "301"})
        self.assertEqual(state.reminders()["300"]["_case_started_at"], 1700000000 * 1_000_000)
        self.assertEqual(set(state.ticket_activity()), {400, 401})
        self.assertEqual(state.ai_messages(), source["ai_messages"])
        rows = await state.events.read()
        # Every open trial with a readable start has its start; nothing is rebuilt for reminders.
        self.assertEqual([(row["kind"], row["case_key"], row["subject_discord_id"], row["actor_kind"],
                           row["occurred_at"], row["detail"]) for row in rows][0],
                         ("trial_started", f"200:{START_US}", 42, "import", START_US, {"days": 7}))
        self.assertEqual({row["source_channel_id"] for row in rows}, {200, 201})
        before = state.state
        self.paths["trials"].write_text(json.dumps({"999": {"start": START, "applicant_id": 1}}))
        restarted = await self.new_state()
        self.assertEqual(restarted.state, before)
        self.assertEqual(await restarted.events.read(), rows)
        # The restart rewrote the mirror from SQLite, undoing the edit above.
        self.assertEqual(json.loads(self.paths["trials"].read_text()), source["trials"])

    async def test_bad_open_work_files_refuse_the_import_but_bad_ai_ids_do_not(self):
        for payload in ("{", "[]"):
            self.paths["reminders"].write_text(payload)
            state = RecruitmentState(EventsStore(self.root / f"bad{len(payload)}.sqlite3"), self.paths)
            with self.assertRaises(ValueError):
                await state.startup()
            self.assertIsNone(await state.events.call(load_state, "recruitment"))
        self.paths["reminders"].unlink()
        self.paths["ai_messages"].write_text("{")
        with self.assertLogs("elbow_helper.features.recruitment.state", level="ERROR"):
            state = await self.new_state()
        self.assertEqual(state.ai_messages(), {})
        self.assertIsNotNone(await state.events.call(load_state, "recruitment"))

    async def test_trial_case_runs_from_start_through_expiry_to_follow_up(self):
        owner = await self.new_owner()
        result = await owner.start_trial_for_accept(self.ticket, 7, 42, actor_id=7, via="button")
        self.assertTrue(result.started)
        trial = owner.recruitment_state.trials()["200"]
        self.assertEqual(json.loads(self.paths["trials"].read_text()), {"200": trial})
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
        self.assertEqual(json.loads(self.paths["reminders"].read_text())["200"]["resolved_by"], 7)

    async def test_restart_mid_expiry_keeps_the_reminder_and_finishes_once(self):
        self.write_legacy({"trials": {"200": legacy()["trials"]["200"]}})
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
        self.write_legacy({"trials": {"200": legacy()["trials"]["200"]}})
        owner = await self.new_owner()
        events = owner.recruitment_state.events
        await events.call(lambda c: c.execute(
            "CREATE TRIGGER fail_end BEFORE INSERT ON workflow_events WHEN NEW.kind='trial_ended' "
            "BEGIN SELECT RAISE(ABORT,'failed'); END"))
        with self.assertRaises(sqlite3.IntegrityError):
            await owner.complete_trial_end(200, 42, self.recruiter)
        self.assertIn("200", owner.recruitment_state.trials())
        self.assertIn("200", json.loads(self.paths["trials"].read_text()))
        await events.call(lambda c: c.execute("DROP TRIGGER fail_end"))
        owner = await self.new_owner()
        self.assertIn("200", owner.recruitment_state.trials())
        self.assertTrue((await owner.complete_trial_end(200, 42, self.recruiter)).ended)
        row = (await owner.recruitment_state.events.read(kind="trial_ended"))[0]
        self.assertEqual((row["detail"], row["via"], row["actor_kind"]), ({"how": "early"}, "button", "member"))

    async def test_applicant_leaving_ends_their_trial_automatically(self):
        self.write_legacy({"trials": legacy()["trials"]})
        owner = await self.new_owner()
        await owner.on_member_remove(SimpleNamespace(id=42))
        self.assertNotIn("200", owner.recruitment_state.trials())
        row, = await owner.recruitment_state.events.read(kind="trial_ended")
        self.assertEqual((row["subject_discord_id"], row["actor_kind"], row["detail"]),
                         (42, "automatic", {"how": "applicant_left"}))

    async def test_two_resolutions_of_one_reminder_record_one_event(self):
        self.write_legacy({"reminders": {"300": legacy()["reminders"]["300"]}})
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

    async def test_mirror_failure_keeps_sqlite_and_the_next_start_rewrites_it(self):
        owner = await self.new_owner()
        with patch("elbow_helper.features.recruitment.state.write_json_atomic", side_effect=OSError("full")):
            with self.assertLogs("elbow_helper.features.recruitment.state", level="ERROR"):
                await owner.start_trial_for_accept(self.ticket, 7, 42, actor_id=7)
        self.assertEqual(json.loads(self.paths["trials"].read_text()), {})
        state = await self.new_state()
        self.assertEqual(json.loads(self.paths["trials"].read_text()), state.trials())
        self.assertEqual(set(state.trials()), {"200"})

    async def test_ticket_and_ai_records_round_trip_and_feed_the_typed_reads(self):
        self.write_legacy({"ticket_activity": legacy()["ticket_activity"]})
        state = await self.new_state()
        activity = state.ticket_activity()
        activity[400]["last_applicant_message_id"] = 10
        await state.save_ticket_activity(activity)
        await state.save_ai_messages({"901": {"channel_id": 1, "created_at": "x"}})
        state = await self.new_state()
        self.assertEqual(state.ticket_activity()[400]["last_applicant_message_id"], 10)
        self.assertEqual(json.loads(self.paths["ticket_activity"].read_text())["400"]["last_applicant_message_id"], 10)
        self.assertEqual(set(json.loads(self.paths["ai_messages"].read_text())), {"901"})
        queries = RecruitmentQueries(state.trials, reminder_loader=state.reminders,
                                     ticket_activity_loader=state.ticket_activity_records)
        self.assertEqual([row.ticket_channel_id for row in queries.ticket_reminders()], [400, 401])

    async def test_delete_member_removes_their_open_trial_reminder_history_and_attribution(self):
        self.write_legacy({"trials": legacy()["trials"], "reminders": legacy()["reminders"]})
        state = await self.new_state()
        await state.record_event(decision_event("declined", 300, 45, actor_id=8, via="command"))
        await state.events.delete_member(42)
        await state.events.delete_member(45)
        await state.events.delete_member(7)
        self.assertNotIn("200", state.trials())
        self.assertEqual(set(state.reminders()), {"301"})
        self.assertIsNone(state.reminders()["301"]["resolved_by"])
        self.assertEqual({row["subject_discord_id"] for row in await state.events.read()}, {43})
        self.assertEqual(json.loads(self.paths["reminders"].read_text()), state.reminders())

    async def test_vocabulary_rejects_free_text_and_unknown_values(self):
        state = await self.new_state()
        accepted = decision_event("accepted", 200, 42, actor_id=7, via="command",
                                  detail={"clans": ["BEH"], "trial_days": 7})
        for change in ({"detail": {"clans": ["words"], "trial_days": 7}},
                       {"detail": {"clans": ["BEH", "BEH"], "trial_days": 7}},
                       {"detail": {"clans": ["BEH"], "trial_days": 7, "note": "words"}},
                       {"detail": {"clans": ["BEH"], "trial_days": 0}},
                       {"kind": "trial_ended", "detail": {"how": "words"}},
                       {"case_key": "200"}, {"kind": "declined"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                await state.events.call(record, replace(accepted, **change))
        self.assertTrue(await state.events.call(record, accepted))
        self.assertFalse(await state.events.call(record, accepted))


if __name__ == "__main__":
    unittest.main()
