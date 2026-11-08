"""Retired files are never opened, rewritten or required by workflow startup."""

from contextlib import chdir
from copy import deepcopy
from pathlib import Path
import sqlite3
from tests.temp_directory import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from elbow_helper.data.events import EventsStore, initialize_state, load_state, save_state
from elbow_helper.features.clan_transfers.state import TransferState
from elbow_helper.features.examination.state import ExaminationStateStore
from elbow_helper.features.hibernation.state import HibernationState
from elbow_helper.features.leadership_news import state as news
from elbow_helper.features.member_lifecycle.state import LifecycleState
from elbow_helper.features.recruitment.state import RecruitmentState
from elbow_helper.features.support_tickets.state import TicketState


WORKFLOWS = {
    "transfers": TransferState,
    "examination": ExaminationStateStore,
    "hibernation": HibernationState,
    "lifecycle": LifecycleState,
    "recruitment": RecruitmentState,
    "support": TicketState,
}
OLD_FILES = (
    "clan_health/clan_health.db", "snapshot_intel/snapshot_intel.json",
    "leadership_news/state.json", "clan_transfers/transfer_queue.json",
    "support/created_tickets.json", "hibernation/hibernation.json",
    "recruitment/trial_data.json", "recruitment/trial_reminders.json",
    "recruitment/ticket_last_activity.json", "recruitment/applicant_messages.json",
    "examination/examination_state.json",
)


class RetiredWorkflowFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(chdir(self.root))
        self.events = EventsStore(self.root / "events.sqlite3")

    async def start(self, workflow, events=None):
        events = events or self.events
        if workflow == news.WORKFLOW:
            return await events.call(news.startup)
        owner = WORKFLOWS[workflow](events)
        await owner.startup()
        if isinstance(owner, ExaminationStateStore):
            self.addCleanup(owner.close)
        return owner

    async def test_defaults_are_persisted_without_files_or_import_events(self):
        for workflow in (*WORKFLOWS, news.WORKFLOW):
            with self.subTest(workflow=workflow):
                await self.start(workflow)
                saved = await self.events.call(load_state, workflow)
                self.assertIsInstance(saved, dict)
                await self.start(workflow, EventsStore(self.events.path))
                self.assertEqual(await self.events.call(load_state, workflow), saved)
        self.assertEqual(await self.events.read(), [])
        self.assertFalse(any(path.suffix == ".json" for path in self.root.rglob("*")))

    async def test_failed_initialization_rolls_back_and_retries_for_every_workflow(self):
        for workflow in (*WORKFLOWS, news.WORKFLOW):
            with self.subTest(workflow=workflow):
                original = save_state
                def fail(connection, kind, payload):
                    original(connection, kind, payload)
                    raise sqlite3.OperationalError("failed after defaults")
                with patch("elbow_helper.data.events.save_state", side_effect=fail):
                    with self.assertRaises(sqlite3.OperationalError):
                        await self.start(workflow)
                self.assertIsNone(await self.events.call(load_state, workflow))
                await self.start(workflow)
                self.assertIsNotNone(await self.events.call(load_state, workflow))
        self.assertEqual(await self.events.read(), [])

    async def test_existing_state_and_old_files_survive_start_change_delete_and_restart(self):
        files = [self.root / "data" / name for name in OLD_FILES]
        for index, path in enumerate(files):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{corrupt old file" + bytes([index]))
        before = {path: path.read_bytes() for path in files}
        owners = {workflow: await self.start(workflow) for workflow in WORKFLOWS}
        await self.start(news.WORKFLOW)
        # Exercise every lasting writer, including the deletion callbacks.
        await owners["transfers"].change(lambda state: (state.update(global_board_message_id=77), ()))
        await owners["hibernation"].save_fallback_info(78)
        await owners["support"].register(123, {"owner": 42, "source": "open"}, actor_id=7)
        await owners["recruitment"].save_ai_messages({"79": {"channel_id": 80}})
        await owners["lifecycle"].update({"ticket_log_last_message_id": "81"})
        owners["examination"].state["panel_message_id"] = 82
        self.assertTrue(await owners["examination"].flush())
        await self.events.call(news.change, lambda connection, state: state.update(legacy_receipts={
            "83": {"channel_id": 84, "message_id": 85, "guild_id": 86}}))
        await self.events.delete_member(42)
        saved = {workflow: deepcopy(await self.events.call(load_state, workflow))
                 for workflow in (*WORKFLOWS, news.WORKFLOW)}
        for workflow in saved:
            await self.start(workflow, EventsStore(self.events.path))
            self.assertEqual(await self.events.call(load_state, workflow), saved[workflow])
        self.assertEqual({path: path.read_bytes() for path in files}, before)
        self.assertEqual((await self.events.call(news.snapshot))["published"]["83"]["message_id"], 85)

    async def test_defaults_run_off_the_event_loop_and_never_replace_existing_state(self):
        thread = threading.get_ident()
        seen = []
        def default():
            seen.append(threading.get_ident())
            return {"marker": 99}
        self.assertEqual(await self.events.call(initialize_state, "support", default), {"marker": 99})
        self.assertNotEqual(seen, [thread])
        await self.events.call(initialize_state, "support", lambda: self.fail("replaced saved state"))
        self.assertEqual(len(seen), 1)
