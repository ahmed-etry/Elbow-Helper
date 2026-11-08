from datetime import datetime, timezone
import json
from pathlib import Path
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import discord

from elbow_helper.configuration.channels import EXAMINATION_TICKET_CATEGORY
from elbow_helper.data.events import EventsStore
from elbow_helper.features.examination.followups import ExaminationFollowupMixin
from elbow_helper.features.examination.state import ExaminationStateStore

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
        store = ExaminationStateStore(EventsStore(self.db))
        await store.startup()
        self.addCleanup(store.close)
        return store

    async def rows(self, store=None):
        return await (store or self.store).events.call(lambda c: [tuple(row) for row in c.execute(
            "SELECT ticket_channel_id,case_json,updated_at FROM examination_cases ORDER BY 1")])

    async def kinds(self, store=None):
        return [row["kind"] for row in await (store or self.store).events.read(workflow="examination")]


    async def test_a_change_saves_only_its_own_case(self):
        self.store.cases()[str(TICKET)] = case()
        self.store.cases()[str(OTHER)] = case(OTHER)
        with patch("elbow_helper.data.events.examination.now_us", return_value=100):
            self.assertTrue(await self.store.flush())
        before = dict((row[0], row) for row in await self.rows())
        self.store.case(TICKET)["intake_state"] = "selecting_from"
        with patch("elbow_helper.data.events.examination.now_us", return_value=200):
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
        self.assertEqual((await self.started()).cases(), self.store.cases())
        self.assertNotIn("closed", await self.kinds())


class ExaminationHandlerHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_examiner_reply_in_the_ticket_records_who_reviewed(self):
        directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(directory.cleanup)
        store = ExaminationStateStore(EventsStore(Path(directory.name) / "events.sqlite3"))
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


if __name__ == "__main__":
    unittest.main()
