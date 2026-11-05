import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.events import EventsStore, load_state
from elbow_helper.data.events.testing import MemoryEventsStore
from elbow_helper.features.clan_transfers.cog import ClanTransfers
from elbow_helper.features.clan_transfers.state import TransferState, build_default_state
from elbow_helper.infrastructure.persistence import read_json, write_json_atomic


class ClanTransferEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory(dir=Path(__file__).parents[2])
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "events.sqlite3"
        self.legacy = Path(self.directory.name) / "transfer_queue.json"
        self.events = EventsStore(self.path)
        self.now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
        self.cog = ClanTransfers(SimpleNamespace(workflow_events=self.events))
        self.cog._now = lambda: self.now
        self.cog.state_store = TransferState(self.events, self.legacy, clock=lambda: self.now)
        await self.cog.state_store.startup()
        self.cog.ensure_queue_message = AsyncMock()
        self.cog.ensure_global_board = AsyncMock()
        self.cog._send_ping = AsyncMock()
        self.cog._fetch_channel = AsyncMock(return_value=None)
        self.cog._is_cwl_season_transfer_window = lambda: True

    async def stored(self):
        return await self.events.call(load_state, "transfers")

    async def test_request_cancel_expire_and_clear_keep_state_events_and_json_together(self):
        self.assertEqual(await self.cog.request_transfer("BEH", 41), "Added you to the BEH transfer queue.")
        self.assertEqual(await self.cog.cancel_transfer("BEH", 41), "Cancelled your request for BEH.")
        self.now += timedelta(seconds=1)
        await self.cog.request_transfer("BEH", 42, via="agent")
        self.now += timedelta(hours=12)
        self.assertEqual(await self.cog._expire_stale_requests("BEH"), 1)
        self.now += timedelta(seconds=1)
        await self.cog.request_transfer("BEH", 43)
        await self.cog.request_transfer("BEH", 44)
        reply = await self.cog.clear_transfer_queue("BEH", actor_discord_id=45, via="button")
        self.assertEqual(reply, "Cleared **2** transfer requests for BEH.")
        rows = await self.events.read(workflow="transfers")
        self.assertEqual([row["kind"] for row in rows], [
            "transfer_requested", "transfer_cancelled", "transfer_requested", "transfer_expired",
            "transfer_requested", "transfer_requested", "queue_cleared", "queue_cleared"])
        self.assertEqual(rows[2]["actor_kind"], "agent")
        self.assertEqual(rows[3]["detail"], {"after_hours": 12})
        self.assertEqual(rows[-1]["actor_discord_id"], 45)
        self.assertEqual(rows[-1]["via"], "button")
        self.assertEqual(rows[-1]["detail"], {"count": 2})
        self.assertEqual(await self.stored(), self.cog.state)
        self.assertEqual(read_json(self.legacy), self.cog.state)
        self.assertEqual(self.cog.state["clans"]["BEH"]["pending"], [])

    async def test_duplicate_request_and_repeated_cancel_clear_expiry_add_no_events(self):
        await self.cog.request_transfer("BEH", 41)
        await self.cog.request_transfer("BEH", 41)
        self.assertEqual(len(await self.events.read()), 1)
        await self.cog.cancel_transfer("BEH", 41)
        await self.cog.cancel_transfer("BEH", 41)
        await self.cog.clear_transfer_queue("BEH")
        await self.cog._expire_stale_requests("BEH")
        self.assertEqual(len(await self.events.read()), 2)

    async def test_imports_only_open_requests_once_and_preserves_message_ids(self):
        state = build_default_state()
        state["global_board_message_id"] = 10
        state["clans"]["BEH"].update(queue_message_id=11, last_ping_message_id=12, pending=[
            {"user_id": 41, "created_at": (self.now - timedelta(hours=1)).isoformat()},
            {"user_id": 42, "created_at": (self.now - timedelta(hours=12)).isoformat()}])
        write_json_atomic(self.legacy, state)
        fresh = EventsStore(self.path.parent / "import.sqlite3")
        moved = TransferState(fresh, self.legacy, clock=lambda: self.now)
        await moved.startup()
        rows = await fresh.read()
        self.assertEqual([(r["subject_discord_id"], r["actor_kind"], r["via"]) for r in rows], [(41, "import", "import")])
        self.assertEqual(moved.state["global_board_message_id"], 10)
        self.assertEqual(moved.state["clans"]["BEH"]["queue_message_id"], 11)
        def clear(state):
            state["clans"]["BEH"]["pending"] = []
            return None, []
        await moved.change(clear)
        write_json_atomic(self.legacy, state)
        restarted = TransferState(fresh, self.legacy, clock=lambda: self.now)
        await restarted.startup()
        self.assertEqual(restarted.state["clans"]["BEH"]["pending"], [])
        self.assertEqual(len(await fresh.read()), 1)
        self.assertEqual(read_json(self.legacy), restarted.state)

    async def test_failed_import_rolls_back_both_state_and_imported_events(self):
        state = build_default_state()
        state["clans"]["BEH"]["pending"] = [{"user_id": 41, "created_at": self.now.isoformat()}]
        write_json_atomic(self.legacy, state)
        original = self.legacy.read_bytes()
        fresh = EventsStore(self.path.parent / "failed-import.sqlite3")
        moved = TransferState(fresh, self.legacy, clock=lambda: self.now)
        with patch("elbow_helper.features.clan_transfers.state.save_state", side_effect=sqlite3.OperationalError("failed")):
            with self.assertRaises(sqlite3.OperationalError):
                await moved.startup()
        self.assertIsNone(await fresh.call(load_state, "transfers"))
        self.assertEqual(await fresh.read(), [])
        self.assertEqual(self.legacy.read_bytes(), original)
        await moved.startup()
        self.assertEqual(len(await fresh.read()), 1)

    async def test_corrupt_import_is_refused_without_an_import_marker(self):
        self.legacy.write_text("{", encoding="utf-8")
        fresh = EventsStore(self.path.parent / "corrupt.sqlite3")
        with self.assertRaises(json.JSONDecodeError):
            await TransferState(fresh, self.legacy).startup()
        self.assertIsNone(await fresh.call(load_state, "transfers"))

    async def test_each_failed_action_leaves_memory_state_events_and_mirror_unchanged(self):
        for operation in ("request", "cancel", "expire", "clear"):
            with self.subTest(operation=operation):
                if operation != "request":
                    self.now += timedelta(seconds=1)
                    await self.cog.request_transfer("BEH", 41)
                before = deepcopy(self.cog.state)
                mirror = self.legacy.read_bytes()
                history = await self.events.read()
                await self.events.call(lambda connection: connection.execute(
                    "CREATE TRIGGER fail_event BEFORE INSERT ON workflow_events BEGIN SELECT RAISE(ABORT,'failed'); END"))
                try:
                    with self.assertRaises(sqlite3.IntegrityError):
                        if operation == "request":
                            await self.cog.request_transfer("BEH", 41)
                        elif operation == "cancel":
                            await self.cog.cancel_transfer("BEH", 41)
                        elif operation == "expire":
                            self.now += timedelta(hours=12)
                            await self.cog._expire_stale_requests("BEH")
                        else:
                            await self.cog.clear_transfer_queue("BEH")
                finally:
                    await self.events.call(lambda connection: connection.execute("DROP TRIGGER fail_event"))
                self.assertEqual(await self.stored(), before)
                self.assertEqual(self.cog.state, before)
                self.assertEqual(await self.events.read(), history)
                self.assertEqual(self.legacy.read_bytes(), mirror)
                await self.cog.clear_transfer_queue("BEH")

    async def test_mirror_failure_keeps_committed_work_and_restart_retries_mirror(self):
        with patch("elbow_helper.features.clan_transfers.state.write_json_atomic", side_effect=OSError("failed")):
            with self.assertLogs("elbow_helper.features.clan_transfers.state", level="ERROR"):
                await self.cog.request_transfer("BEH", 41)
        self.assertEqual(len(await self.events.read()), 1)
        restarted = TransferState(EventsStore(self.path), self.legacy, clock=lambda: self.now)
        await restarted.startup()
        self.assertEqual(read_json(self.legacy), self.cog.state)

    async def test_concurrent_clans_keep_their_events_and_latest_mirror(self):
        await asyncio.gather(self.cog.request_transfer("BEH", 41), self.cog.request_transfer("BEC", 42))
        self.assertEqual(len(await self.events.read()), 2)
        self.assertEqual(read_json(self.legacy), await self.stored())
        self.assertEqual(len(self.cog.state["clans"]["BEH"]["pending"]), 1)
        self.assertEqual(len(self.cog.state["clans"]["BEC"]["pending"]), 1)

    async def test_cancelled_worker_save_finishes_state_event_and_mirror(self):
        started, release = threading.Event(), threading.Event()
        from elbow_helper.features.clan_transfers import state as module
        original = module.record
        def slow(*args):
            original(*args)
            started.set()
            release.wait(3)
        with patch.object(module, "record", side_effect=slow):
            task = asyncio.create_task(self.cog.request_transfer("BEH", 41))
            try:
                while not started.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(await self.events.read()), 1)
        self.assertEqual(read_json(self.legacy), self.cog.state)
        self.assertEqual(await self.stored(), self.cog.state)

    async def test_delete_member_scrubs_memory_and_revert_mirror_without_resurrection(self):
        await self.cog.request_transfer("BEH", 41)
        await self.cog.request_transfer("BEC", 42)
        await self.events.delete_member(41)
        self.assertEqual(self.cog.state["clans"]["BEH"]["pending"], [])
        self.assertEqual(read_json(self.legacy), self.cog.state)
        await self.cog._set_board_message(99)
        self.assertEqual([r["subject_discord_id"] for r in await self.events.read()], [42])
        self.assertEqual((await self.stored())["clans"]["BEH"]["pending"], [])

    async def test_memory_double_can_fail_after_state_and_event_writes(self):
        memory = MemoryEventsStore()
        self.addCleanup(memory.close)
        self.cog.state_store = TransferState(memory, self.legacy, clock=lambda: self.now)
        await self.cog.state_store.startup()
        memory.fail_next_write = True
        with self.assertRaises(sqlite3.OperationalError):
            await self.cog.request_transfer("BEH", 41)
        self.assertEqual(await memory.read(), [])
        self.assertEqual(self.cog.state["clans"]["BEH"]["pending"], [])
