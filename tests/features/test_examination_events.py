import asyncio
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import discord

from elbow_helper.configuration.channels import EXAMINATION_TICKET_CATEGORY
from elbow_helper.data.events import EventsStore, load_state, record
from elbow_helper.data.events.schema import SCHEMA_VERSION, _create_history
from elbow_helper.features.examination.followups import ExaminationFollowupMixin
from elbow_helper.features.examination.queries import ExaminationQueries
from elbow_helper.features.examination.state import ExaminationStateStore, examination_event

# Discord channel IDs carry their creation time.
TICKET = 1_200_000_000_000_000_000
OTHER = TICKET + 1


def iso(second):
    return datetime.fromtimestamp(1_700_000_000 + second, timezone.utc).isoformat()


def case(channel_id=TICKET, **values):
    return {"type": "clan_promo", "created_at": iso(0), "opener_id": 41, "ticket_channel_id": channel_id,
            "availability": "", "availability_structured": None, "routing_message_id": None,
            "routing_inflight": False, "pinged_ids": [], "stage": "pending", "responded": False,
            "intake_state": "pending", "exam_required": None, "from_clan": None, "to_clan": None,
            "elder_reason": "free text the applicant wrote", **values}


class ExaminationHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = self.root / "events.sqlite3"
        self.path = self.root / "examination_state.json"
        self.store = await self.started()

    async def started(self, path=None):
        store = ExaminationStateStore(EventsStore(self.db), self.path if path is None else path)
        await store.startup()
        self.addCleanup(store.close)
        return store

    async def rows(self, store=None):
        return await (store or self.store).events.call(lambda c: [tuple(row) for row in c.execute(
            "SELECT ticket_channel_id,case_json,updated_at FROM examination_cases ORDER BY 1")])

    async def kinds(self, store=None):
        return [row["kind"] for row in await (store or self.store).events.read(workflow="examination")]

    async def test_first_start_imports_open_cases_once_and_keeps_the_old_shape(self):
        legacy = {"examiner_roster": {"7": {"status": "Active", "th_levels": [15]}},
                  "cases": {str(TICKET): case(), str(OTHER): case(OTHER, type="elder_promo"),
                            "not-a-channel": {"type": "clan_promo"}},
                  "panel_message_id": 99, "deprecated_routing_messages": [{"message_id": 5}],
                  "timezone_preferences": {}}
        path = self.root / "legacy.json"
        path.write_text(json.dumps(legacy))
        with self.assertLogs("elbow_helper.features.examination.state", level="INFO") as logs:
            store = ExaminationStateStore(EventsStore(self.root / "import.sqlite3"), path)
            await store.startup()
        self.addCleanup(store.close)
        self.assertIn("Imported 2 open examination cases (2 start events); skipped 1", logs.output[0])
        self.assertEqual(store.cases(), {key: value for key, value in legacy["cases"].items()
                                         if key != "not-a-channel"})
        self.assertEqual(store.examiner_roster(), legacy["examiner_roster"])
        self.assertEqual(store.state["panel_message_id"], 99)
        rows = await store.events.read(workflow="examination")
        self.assertEqual([(row["kind"], row["actor_kind"], row["via"]) for row in rows], [("started", "import", "import")] * 2)
        self.assertEqual({row["detail"]["case_type"] for row in rows}, {"clan_promo", "elder_promo"})
        self.assertTrue(all(row["subject_discord_id"] == 41 for row in rows))
        mirrored = json.loads(path.read_text())
        self.assertEqual(mirrored["cases"], store.cases())
        restarted = ExaminationStateStore(EventsStore(store.events.path), path)
        path.write_text(json.dumps({"cases": {"5": case(5)}}))
        await restarted.startup()
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.state, store.state)
        self.assertEqual(await restarted.events.read(), rows)

    async def test_a_change_saves_only_its_own_case(self):
        self.store.cases()[str(TICKET)] = case()
        self.store.cases()[str(OTHER)] = case(OTHER)
        self.assertTrue(await self.store.flush())
        before = dict((row[0], row) for row in await self.rows())
        await asyncio.sleep(0.01)
        self.store.case(TICKET)["intake_state"] = "selecting_from"
        self.assertTrue(await self.store.flush())
        after = dict((row[0], row) for row in await self.rows())
        self.assertEqual(after[OTHER], before[OTHER])
        self.assertNotEqual(after[TICKET], before[TICKET])
        self.assertEqual(json.loads(after[TICKET][1])["intake_state"], "selecting_from")
        # Nothing changed: no write at all.
        with patch.object(self.store.events, "call", side_effect=AssertionError("wrote")):
            self.assertTrue(await self.store.flush())

    async def test_each_stage_is_recorded_once_without_text_or_outcome(self):
        cases = self.store.cases()
        cases[str(TICKET)] = case()
        await self.store.flush()
        live = self.store.case(TICKET)
        live.update(intake_state="complete", intake_completed_at=iso(10), exam_required=True,
                    from_clan="BEM", to_clan="BEC")
        await self.store.flush()
        live.update(availability_structured=[{"days": ["mon"], "start": 18.0, "end": 20.0, "timezone": "UTC"}],
                    availability_set_at=iso(20), availability_set_by=41)
        await self.store.flush()
        cases[str(TICKET)] = {**live, "routing_message_id": 555, "routing_created_at": iso(30), "stage": "initial"}
        await self.store.flush()
        live = self.store.case(TICKET)
        live.update(stage="reminder", last_ping_at=iso(40))
        await self.store.flush()
        live.update(responded=True, responded_by=7, responded_at=iso(50))
        await self.store.flush()
        live.update(responded=False, routing_message_id=None, stage="pending", route_change_requested_at=iso(60),
                    route_change_requested_by=8, route_change_via="agent", to_clan="BE1")
        await self.store.flush()
        await self.store.flush()
        rows = await self.store.events.read(workflow="examination")
        self.assertEqual([row["kind"] for row in rows], [
            "started", "answered", "availability_given", "sent_for_review", "followed_up", "reviewed",
            "route_changed"])
        by_kind = {row["kind"]: row for row in rows}
        self.assertEqual(by_kind["answered"]["detail"], {"exam_required": True})
        self.assertEqual(by_kind["answered"]["clan_code"], "BEC")
        self.assertEqual(by_kind["sent_for_review"]["detail"], {"exam_required": True, "missing_answers": False})
        self.assertEqual(by_kind["sent_for_review"]["source_message_id"], 555)
        self.assertEqual(by_kind["followed_up"]["detail"], {"stage": "reminder"})
        self.assertEqual((by_kind["reviewed"]["actor_discord_id"], by_kind["reviewed"]["via"]), (7, "message"))
        self.assertEqual((by_kind["route_changed"]["actor_kind"], by_kind["route_changed"]["detail"]),
                         ("agent", {"reopened": False}))
        self.assertEqual(by_kind["reviewed"]["occurred_at"], 1_700_000_050_000_000)
        self.assertTrue(all(row["subject_discord_id"] == 41 for row in rows))
        self.assertNotIn("free text", json.dumps([row["detail"] for row in rows]))
        cases.pop(str(TICKET))
        await self.store.flush()
        self.assertEqual((await self.kinds())[-1], "closed")
        self.assertEqual(await self.rows(), [])
        self.assertEqual(self.store.stage_history(), {})

    async def test_failed_write_keeps_the_change_and_records_it_once_on_retry(self):
        self.store.cases()[str(TICKET)] = case()
        await self.store.events.call(lambda c: c.execute(
            "CREATE TRIGGER fail_case BEFORE INSERT ON examination_cases BEGIN SELECT RAISE(ABORT,'full'); END"))
        with self.assertLogs("elbow_helper.features.examination.state", level="ERROR"):
            self.assertFalse(await self.store.flush())
        self.assertEqual(await self.rows(), [])
        self.assertEqual(await self.kinds(), [])
        self.assertEqual(self.store.saved_cases(), {})
        self.assertIn(str(TICKET), self.store.cases())
        await self.store.events.call(lambda c: c.execute("DROP TRIGGER fail_case"))
        self.assertTrue(await self.store.flush())
        self.assertTrue(await self.store.flush())
        self.assertEqual(await self.kinds(), ["started"])
        self.assertEqual(len(await self.rows()), 1)

    async def test_failed_event_insert_rolls_back_the_case_too(self):
        self.store.cases()[str(TICKET)] = case()
        await self.store.events.call(lambda c: c.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON workflow_events BEGIN SELECT RAISE(ABORT,'full'); END"))
        with self.assertLogs("elbow_helper.features.examination.state", level="ERROR"):
            self.assertFalse(await self.store.flush())
        self.assertEqual(await self.rows(), [])
        self.assertEqual(await self.store.events.call(load_state, "examination"), {
            "examiner_roster": {}, "panel_message_id": None, "deprecated_routing_messages": [],
            "timezone_preferences": {}})

    async def test_restart_mid_case_resumes_from_the_saved_case(self):
        self.store.cases()[str(TICKET)] = case(intake_state="complete", intake_completed_at=iso(10),
                                               exam_required=False)
        self.store.examiner_roster()["7"] = {"status": "Away"}
        await self.store.flush()
        # Not yet saved when the bot stops: this change is lost, as before the move.
        self.store.case(TICKET)["routing_inflight"] = True
        restarted = await self.started()
        self.assertEqual(restarted.case(TICKET), self.store.saved_cases()[str(TICKET)])
        self.assertFalse(restarted.case(TICKET)["routing_inflight"])
        self.assertEqual(restarted.examiner_roster(), {"7": {"status": "Away"}})
        restarted.case(TICKET).update(routing_message_id=555, routing_created_at=iso(30), stage="initial")
        await restarted.flush()
        self.assertEqual(await self.kinds(restarted), ["started", "answered", "sent_for_review"])
        history = restarted.stage_history()[TICKET]
        self.assertEqual([kind for kind, _ in history], ["started", "answered", "sent_for_review"])

    async def test_save_from_a_handler_coalesces_into_one_background_write(self):
        self.store.cases()[str(TICKET)] = case()
        calls = 0
        original = self.store.events.call
        async def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await original(*args, **kwargs)
        with patch.object(self.store.events, "call", side_effect=counted):
            self.store.save()
            self.store.case(TICKET)["stage"] = "initial"
            self.store.save()
            await self.store._flush_task
        self.assertEqual(calls, 1)
        self.assertEqual(self.store.saved_cases()[str(TICKET)]["stage"], "initial")

    async def test_agent_reads_saved_cases_and_their_stages(self):
        queries = ExaminationQueries(self.store.saved_cases, history=self.store.stage_history)
        self.store.cases()[str(TICKET)] = case()
        self.assertEqual(queries.case_registrations(), ())
        await self.store.flush()
        self.store.case(TICKET)["intake_state"] = "selecting_from"
        row, = queries.case_snapshot(ticket_channel_ids=(TICKET,)).cases
        self.assertEqual(row.intake_status, "pending")
        self.assertEqual([stage.stage for stage in row.stages], ["started"])
        self.assertEqual(row.stages[0].at, iso(0))

    async def test_mirror_failure_recovers_from_sqlite_on_restart(self):
        self.store.cases()[str(TICKET)] = case()
        with patch("elbow_helper.features.examination.state.write_json_atomic", side_effect=OSError("full")):
            with self.assertLogs("elbow_helper.features.examination.state", level="ERROR"):
                self.assertTrue(await self.store.flush())
        restarted = await self.started()
        self.assertEqual(restarted.cases(), self.store.cases())
        self.assertEqual(json.loads(self.path.read_text())["cases"], restarted.cases())

    async def test_deleting_a_member_removes_their_case_profile_and_mentions(self):
        self.store.cases()[str(TICKET)] = case()
        self.store.cases()[str(OTHER)] = case(OTHER, opener_id=43, responded=True, responded_by=41,
                                              pinged_ids=[41, 9], availability_set_by=41)
        self.store.examiner_roster().update({"41": {"status": "Active"}, "9": {"status": "Active"}})
        await self.store.flush()
        unsaved = self.store.case(OTHER)
        unsaved["route_change_requested_by"] = 41
        await self.store.events.delete_member(41)
        self.assertNotIn(str(TICKET), self.store.cases())
        other = self.store.case(OTHER)
        self.assertEqual((other["responded_by"], other["pinged_ids"], other["availability_set_by"],
                          other["route_change_requested_by"]), (None, [9], None, None))
        self.assertEqual(set(self.store.examiner_roster()), {"9"})
        self.assertEqual([json.loads(row[1])["opener_id"] for row in await self.rows()], [43])
        self.assertEqual({row["subject_discord_id"] for row in await self.store.events.read()}, {43})
        await self.store.flush()
        self.assertEqual(json.loads(self.path.read_text())["cases"], self.store.cases())
        self.assertNotIn("closed", await self.kinds())

    async def test_unreadable_old_file_does_not_mark_the_import_done(self):
        for payload in ("{", "[]", '{"cases": []}'):
            self.path.write_text(payload)
            events = EventsStore(self.root / f"bad{len(payload)}.sqlite3")
            store = ExaminationStateStore(events, self.path)
            with self.assertRaises(ValueError):
                await store.startup()
            self.assertIsNone(await events.call(load_state, "examination"))

    async def test_stage_details_reject_free_text(self):
        event = examination_event("followed_up", TICKET, case(), at=1, detail={"stage": "reminder"})
        for detail in ({"stage": "please review"}, {"stage": "reminder", "note": "x"}):
            with self.assertRaises(ValueError):
                await self.store.events.call(record, replace(event, detail=detail))
        started = examination_event("started", TICKET, case(), at=1, detail={"case_type": "words"})
        with self.assertRaises(ValueError):
            await self.store.events.call(record, started)
        # A case with an unknown type still saves, without its start event.
        self.store.cases()[str(TICKET)] = case(type="words")
        with self.assertLogs("elbow_helper.features.examination.state", level="WARNING"):
            self.assertTrue(await self.store.flush())
        self.assertEqual(await self.kinds(), [])
        self.assertEqual(len(await self.rows()), 1)

    async def test_cancelled_save_still_publishes_what_it_committed(self):
        self.store.cases()[str(TICKET)] = case()
        original = self.store.events.call
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            started.set()
            await release.wait()
            return await original(*args, **kwargs)
        with patch.object(self.store.events, "call", side_effect=slow):
            task = asyncio.create_task(self.store.flush())
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIn(str(TICKET), self.store.saved_cases())
        snapshot = deepcopy(self.store.saved_cases())
        self.assertTrue(await self.store.flush())
        self.assertEqual(await self.kinds(), ["started"])
        self.assertEqual(self.store.saved_cases(), snapshot)


class ExaminationHandlerHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_examiner_reply_in_the_ticket_records_who_reviewed(self):
        directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(directory.cleanup)
        store = ExaminationStateStore(EventsStore(Path(directory.name) / "events.sqlite3"),
                                      Path(directory.name) / "state.json")
        await store.startup()
        self.addCleanup(store.close)
        store.cases()[str(TICKET)] = case(routing_message_id=555, stage="initial")
        await store.flush()
        owner = object.__new__(ExaminationFollowupMixin)
        owner._message_ticket_type = lambda message: None
        owner._get_cases, owner._get_case, owner._save = store.cases, store.case, store.save
        channel = MagicMock(spec=discord.TextChannel)
        channel.id, channel.category_id = TICKET, EXAMINATION_TICKET_CATEGORY
        message = SimpleNamespace(guild=object(), channel=channel, author=SimpleNamespace(id=7, bot=False))
        await owner.on_message(message)
        await store._flush_task
        row, = await store.events.read(workflow="examination", kind="reviewed")
        self.assertEqual((row["actor_discord_id"], row["actor_kind"], row["via"]), (7, "member", "message"))
        self.assertTrue(store.saved_cases()[str(TICKET)]["responded"])


class ExaminationSchemaUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_version_one_file_gains_the_case_table_and_keeps_its_history(self):
        directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "events.sqlite3"

        def old_file():
            with closing(sqlite3.connect(path)) as connection:
                _create_history(connection)
                connection.execute("INSERT INTO workflow_state VALUES ('transfers','{}',1)")
                connection.execute("PRAGMA user_version=1")
                connection.commit()
        await asyncio.to_thread(old_file)
        events = EventsStore(path)
        store = ExaminationStateStore(events, Path(directory.name) / "state.json")
        await store.startup()
        self.addCleanup(store.close)
        self.assertEqual(await events.call(load_state, "transfers"), {})
        self.assertEqual(await events.call(lambda c: c.execute("PRAGMA user_version").fetchone()[0]),
                         SCHEMA_VERSION)
        store.cases()[str(TICKET)] = case()
        self.assertTrue(await store.flush())


if __name__ == "__main__":
    unittest.main()
