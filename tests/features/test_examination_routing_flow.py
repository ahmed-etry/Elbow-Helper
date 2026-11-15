from __future__ import annotations

from pathlib import Path
from tests.temp_directory import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from elbow_helper.data.events import EventsStore
from elbow_helper.features.examination.cog import Examination
from elbow_helper.features.examination.state import ExaminationStateStore

TICKET = 1_200_000_000_000_000_000


def clan_case(**values):
    return {
        "type": "clan_promo", "created_at": "2026-11-01T00:00:00+00:00", "opener_id": 41,
        "ticket_channel_id": TICKET, "availability": "", "availability_structured": None,
        "routing_message_id": None, "routing_inflight": False, "pinged_ids": [], "pinged_role_ids": [],
        "stage": "pending", "responded": False, "intake_state": "complete", "exam_required": False,
        "from_clan": "BES", "to_clan": "BE4", "th_level": 16, **values,
    }


class RoutingMarkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(directory.cleanup)
        self.store = ExaminationStateStore(EventsStore(Path(directory.name) / "events.sqlite3"))
        await self.store.startup()
        self.addCleanup(self.store.close)

    def owner(self, routing_channel):
        owner = object.__new__(Examination)
        owner.bot = MagicMock()
        owner.state_store = self.store
        owner.state = self.store.state
        owner._pending_ticket_retries = {}
        owner._pending_ticket_notified = set()
        owner._pending_ticket_failed = set()
        owner._background_tasks = set()
        owner.find_ticket_opener = AsyncMock(return_value=None)
        owner._latest_applicant_message = AsyncMock(return_value="")
        owner._extract_ticket_fields = AsyncMock(return_value={"th_level": "16"})
        owner._clear_availability_prompt = AsyncMock()
        owner._get_routing_channel = AsyncMock(return_value=routing_channel)
        owner._collect_examiners = MagicMock(return_value=[])
        owner._get_examiner_roster = MagicMock(return_value={})
        return owner

    async def test_a_failed_routing_post_does_not_leave_the_ticket_marked_as_routing(self):
        self.store.cases()[str(TICKET)] = clan_case()
        routing_channel = MagicMock(spec=discord.TextChannel)
        routing_channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "down"))
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = TICKET
        owner = self.owner(routing_channel)
        with self.assertRaises(discord.HTTPException):
            await owner.route_ticket(channel, "clan_promo")
        await self.store._flush_task
        routing_channel.send.assert_awaited_once()
        self.assertFalse(self.store.case(TICKET)["routing_inflight"])

    async def test_a_routing_mark_saved_before_a_restart_is_cleared_on_startup(self):
        self.store.cases()[str(TICKET)] = clan_case(routing_inflight=True)
        self.store.cases()[str(TICKET + 1)] = clan_case(ticket_channel_id=TICKET + 1, routing_message_id=555)
        await self.store.flush()
        restarted_store = ExaminationStateStore(self.store.events)
        owner = object.__new__(Examination)
        owner.state_store = restarted_store
        await owner.cog_load()
        self.addCleanup(restarted_store.close)
        self.assertFalse(restarted_store.case(TICKET)["routing_inflight"])
        await restarted_store.flush()
        self.assertFalse(restarted_store.saved_cases()[str(TICKET)]["routing_inflight"])


if __name__ == "__main__":
    unittest.main()
