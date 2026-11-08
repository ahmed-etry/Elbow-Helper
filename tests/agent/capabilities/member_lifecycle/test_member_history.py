"""The agent's member history: record stays with lifecycle sources and reasons, never message text."""

from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.capabilities.member_lifecycle import test_agent_member_lifecycle as lifecycle_fixtures
from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.agent.tool_support.adapters.member_lifecycle.history import read_member_history
from elbow_helper.data.events.testing import MemoryEventsStore
from elbow_helper.features.member_lifecycle.queries import LifecycleRecordSource, MemberLifecycleQueries
from elbow_helper.features.member_lifecycle.state import LifecycleState

JOINED = 1_790_000_000_000_000
REJOINED = 1_791_000_000_000_000
LEFT = 1_790_500_000_000_000
RECORDED = 1_789_000_000_000_000


class Reads:
    def __init__(self):
        self.stays = {}
        self.fail = False

    async def server_stays(self, member_ids, as_of=None):
        if self.fail:
            raise sqlite3.OperationalError("synthetic failure")
        return {"rows": [{"member_id": member_id, "stays": self.stays.get(member_id, []), "last_message": None}
                         for member_id in member_ids],
                "coverage": {"complete": True, "signals": [{"collection_start": RECORDED}]}}


def iso(at):
    return datetime.fromtimestamp(at / 1_000_000, timezone.utc).isoformat()


class MemberHistoryTests(lifecycle_fixtures.AgentMemberLifecycleTests):
    # Reuse the parent's guild and permissions only; its snapshot tests run there.
    test_snapshot_filters_activity_by_channel_access_and_retains_sources = None
    test_overseeing_access_is_required_before_state_read = None
    test_stale_activity_channel_is_not_fetched_or_retained_as_evidence = None
    test_activity_permission_loss_hides_retained_snapshot = None

    async def asyncSetUp(self):
        self.events = MemoryEventsStore()
        self.addCleanup(self.events.close)
        self.lifecycle = LifecycleState(self.events)
        await self.lifecycle.startup()
        self.reads = Reads()
        object.__setattr__(self.context, "member_lifecycle_queries", MemberLifecycleQueries(
            self.lifecycle.snapshot, source=LifecycleRecordSource(self.lifecycle, lambda: self.reads)))
        # Any message search or history read fails the test.
        self.search = SimpleNamespace(search_page=AsyncMock(side_effect=AssertionError("searched")),
                                      history_page=AsyncMock(side_effect=AssertionError("scanned")))
        object.__setattr__(self.context, "message_search", self.search)

    async def test_stays_come_from_the_record_with_source_reason_and_join_message(self):
        await self.lifecycle.joined(7, JOINED, "Referral", 501)
        await self.lifecycle.left(7, JOINED, "kicked")
        self.reads.stays[7] = [
            # Discord's join time differs below a millisecond between its gateway and member list.
            {"from_at": JOINED + 3, "to_at": LEFT, "discord_joined_at": JOINED + 400, "start_basis": "gateway_join",
             "end_basis": "gateway_leave", "last_observed_at": LEFT},
            {"from_at": REJOINED + 3, "to_at": None, "discord_joined_at": REJOINED, "start_basis": "scan",
             "end_basis": None, "last_observed_at": REJOINED + 3},
        ]
        result = await read_member_history(self.context, {"member_id": 7})
        self.assertEqual([(row["event"], row["basis"], row["platform"], row["reason"], row["message_id"])
                          for row in result["entries"]],
                         [("join", "gateway_join", "Referral", None, 501),
                          ("leave", "gateway_leave", None, "kicked", None),
                          ("join", "scan", None, None, None)])
        self.assertEqual([row["timestamp"] for row in result["entries"]], [iso(JOINED + 400), iso(LEFT), iso(REJOINED)])
        self.assertEqual(result["entries"][0]["source"], f"https://discord.com/channels/1/{OVERSEEING_TERRACE}/501")
        self.assertTrue(result["search_is_exhaustive"])
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(result["coverage"]["recorded_since"], iso(RECORDED))
        self.assertEqual(self.context.state.source_channels, {OVERSEEING_TERRACE})
        self.search.search_page.assert_not_awaited()
        self.search.history_page.assert_not_awaited()

    async def test_a_departed_member_with_no_events_still_has_their_record_stays(self):
        self.reads.stays[700] = [{"from_at": JOINED, "to_at": LEFT, "discord_joined_at": None, "start_basis": "scan",
                                  "end_basis": "complete_scan_absence", "last_observed_at": JOINED}]
        result = await read_member_history(self.context, {"member_id": 700})
        self.assertEqual([(row["event"], row["basis"]) for row in result["entries"]],
                         [("join", "scan"), ("leave", "complete_scan_absence")])

    async def test_hidden_source_cursor_and_unavailable_record_are_refused(self):
        self.permissions[OVERSEEING_TERRACE] = False
        self.assertIn("error", await read_member_history(self.context, {"member_id": 7}))
        self.permissions[OVERSEEING_TERRACE] = True
        self.assertEqual(await read_member_history(self.context, {"member_id": 7, "cursor": "old"}),
                         {"error": "That continuation cursor does not match this member history."})
        self.reads.fail = True
        self.assertEqual(await read_member_history(self.context, {"member_id": 7}),
                         {"error": "Stored member lifecycle observations could not be read."})
        self.assertEqual(self.context.state.source_channels, set())
