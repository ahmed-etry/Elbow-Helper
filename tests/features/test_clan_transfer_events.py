from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.data.events import EventsStore, load_state
from elbow_helper.features.clan_transfers.cog import ClanTransfers
from elbow_helper.features.clan_transfers.state import TransferState


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
        self.cog.state_store = TransferState(self.events)
        await self.cog.state_store.startup()
        self.cog.ensure_queue_message = AsyncMock()
        self.cog.ensure_global_board = AsyncMock()
        self.cog._send_ping = AsyncMock()
        self.cog._fetch_channel = AsyncMock(return_value=None)
        self.cog._is_cwl_season_transfer_window = lambda: True

    async def stored(self):
        return await self.events.call(load_state, "transfers")

    async def test_request_cancel_expire_and_clear_keep_state_events_together(self):
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
        self.assertEqual(await self.stored(), self.cog.state)
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


    async def test_each_failed_action_leaves_memory_state_events_unchanged(self):
        for operation in ("request", "cancel", "expire", "clear"):
            with self.subTest(operation=operation):
                if operation != "request":
                    self.now += timedelta(seconds=1)
                    await self.cog.request_transfer("BEH", 41)
                before = deepcopy(self.cog.state)
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
                await self.cog.clear_transfer_queue("BEH")


    async def test_delete_member_scrubs_memory_and_saved_state_without_resurrection(self):
        await self.cog.request_transfer("BEH", 41)
        await self.cog.request_transfer("BEC", 42)
        await self.events.delete_member(41)
        self.assertEqual(self.cog.state["clans"]["BEH"]["pending"], [])
        self.assertEqual(await self.stored(), self.cog.state)
        await self.cog._set_board_message(99)
        self.assertEqual([r["subject_discord_id"] for r in await self.events.read()], [42])
        self.assertEqual((await self.stored())["clans"]["BEH"]["pending"], [])
