"""Switching the CWL features from the old code to the family record in the middle of a league.

The router's state file (rotation baselines, posted missed attacks, the last poll) is what the old code leaves
behind; these tests run the router on it over a real record that a collector keeps, including the cases where
the record is a moment behind the war it follows.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.cwl.config import DASHBOARD_THREADS
from elbow_helper.features.cwl.dashboard import CwlDashboardMixin
from elbow_helper.features.cwl.router import CwlRouterMixin

from .test_cwl_record import NOV, SECOND, LeagueFixture, end_of, league_war, now

ROUTER = "elbow_helper.features.cwl.router"
DASHBOARD = "elbow_helper.features.cwl.dashboard"
ONE = {"ONE": "#PY"}


def clock_at(moment_us):
    """The router's datetime, standing at ``moment_us`` (UTC epoch microseconds)."""
    moment = datetime.fromtimestamp(moment_us // SECOND, timezone.utc)

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(moment.timestamp(), timezone.utc if tz is not None else None)

    return Fixed


def old_state(**changes):
    """The router state file as the old code wrote it."""
    state = {"rosters": {}, "roster_names": {}, "last_war_tag": {}, "missed_posted": {}, "missed_pending": {},
             "name_cache": {}, "last_poll_ts": 0, "cwl_board_messages": {}}
    state.update(changes)
    return state


class SwitchRouterTests(LeagueFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.missed, self.rotation = AsyncMock(return_value=True), AsyncMock()

    def router(self, state):
        router = CwlRouterMixin()
        router.state = state
        router.cwl_groups = self.groups
        router._sync_cwl_channel = AsyncMock()
        router.sync_registered_cwl_thread = AsyncMock(return_value=True)
        router._log_missed_api, router._log_rotation_api = self.missed, self.rotation
        router._save_state = AsyncMock()
        return router

    async def poll(self, router, moment):
        with now(moment), patch(f"{ROUTER}.CWL_CLAN_TAGS", ONE), patch(f"{ROUTER}.datetime", clock_at(moment)):
            await router._poll_once()

    async def ended_with_a_miss(self, round_no, tag, at):
        """The collector's check after the round: one member of the lineup never attacked."""
        payload = league_war("inWar", round_no)
        payload["state"] = "warEnded"
        await self.record(payload, kind="cwl_war", scope=tag, at=at)

    async def test_a_war_the_record_shows_ended_late_still_gets_its_missed_attacks_post(self):
        await self.november()
        end = end_of(2)
        router = self.router(old_state(last_poll_ts=end // SECOND - 30, missed_posted={"ONE:#R0": True}))
        # The war is over, but the collector has not checked it since the last battle hour.
        await self.poll(router, end + 30 * SECOND)
        self.missed.assert_not_awaited()
        self.assertEqual(router.state["last_poll_ts"], end // SECOND - 30)
        await self.ended_with_a_miss(2, "#R8", end + 90 * SECOND)
        await self.poll(router, end + 100 * SECOND)
        self.missed.assert_awaited_once()
        self.assertEqual(self.missed.await_args.args[:2], ("ONE", "#R8"))
        self.assertTrue(router.state["missed_posted"]["ONE:#R8"])
        self.assertGreater(router.state["last_poll_ts"], end // SECOND)
        await self.poll(router, end + 160 * SECOND)
        self.missed.assert_awaited_once()

    async def test_a_restart_after_a_war_ended_posts_it_once_when_the_record_catches_up(self):
        await self.november()
        end = end_of(2)
        # The old code last polled before the war ended and went down; the new one starts 3 minutes later,
        # before the collector's first pass.
        router = self.router(old_state(last_poll_ts=end // SECOND - 60, missed_posted={"ONE:#R0": True}))
        await self.poll(router, end + 180 * SECOND)
        self.missed.assert_not_awaited()
        await self.ended_with_a_miss(2, "#R8", end + 190 * SECOND)
        await self.poll(router, end + 240 * SECOND)
        await self.poll(router, end + 300 * SECOND)
        self.missed.assert_awaited_once()

    async def test_wars_posted_by_the_old_code_are_not_posted_again(self):
        await self.november()
        router = self.router(old_state(last_poll_ts=NOV // SECOND, missed_posted={"ONE:#R0": True}))
        # Round 1 ended after the old code's last poll but it had posted it (the file says so).
        await self.poll(router, end_of(1) + 600 * SECOND)
        self.missed.assert_not_awaited()
        self.assertEqual(router.state["missed_posted"], {"ONE:#R0": True})

    async def test_a_lineup_baseline_the_old_code_saved_is_kept_across_the_switch(self):
        await self.november()
        moment = end_of(2) - 1800 * SECOND
        names = {"#P0": "Player #P0", "#P2": "Player #P2"}
        same = old_state(last_poll_ts=moment // SECOND, rosters={"ONE": {"#RC": ["#P0", "#P2"]}},
                         roster_names={"ONE": {"#RC": names}})
        router = self.router(same)
        await self.poll(router, moment)
        self.rotation.assert_not_awaited()
        # The old code saw #P8 in the lineup; the record has the lineup after the swap.
        swapped = old_state(last_poll_ts=moment // SECOND, rosters={"ONE": {"#RC": ["#P0", "#P8"]}},
                            roster_names={"ONE": {"#RC": {"#P0": "Player #P0", "#P8": "Spare"}}})
        router = self.router(swapped)
        await self.poll(router, moment)
        await self.poll(router, moment + 60 * SECOND)
        self.rotation.assert_awaited_once()
        self.assertEqual(self.rotation.await_args.args, ("ONE", ["Player #P2"], ["Spare"]))
        self.assertEqual(sorted(router.state["rosters"]["ONE"]["#RC"]), ["#P0", "#P2"])

    async def test_state_the_old_code_wrote_loads_unchanged(self):
        saved = old_state(last_poll_ts=123, rosters={"ONE": {"#RC": ["#P0"]}}, missed_posted={"ONE:#R0": True},
                          cwl_board_messages={"BE1": {"channel": 5, "message": 6}}, last_war_tag={"ONE": "#R8"})
        with TemporaryDirectory() as folder:
            path = Path(folder) / "router.json"
            path.write_text(json.dumps(saved), encoding="utf-8")
            with patch(f"{ROUTER}.ROUTER_STATE_FILE", path):
                loaded = CwlRouterMixin()._load_state()
        self.assertEqual(loaded, saved)


class DashboardStartTests(unittest.IsolatedAsyncioTestCase):
    def dashboards(self):
        dashboards = CwlDashboardMixin()
        dashboards.bot = object()
        dashboards.clash_client = SimpleNamespace(configured=True)
        dashboards._first_war_check = asyncio.Event()
        dashboards._refresh_dashboard_with_retry = AsyncMock(return_value=True)
        return dashboards

    async def test_the_first_refresh_after_a_start_waits_for_the_first_war_pass(self):
        dashboards = self.dashboards()
        with patch(f"{DASHBOARD}.wait_for_boot_complete", AsyncMock()):
            started = asyncio.create_task(dashboards.before_dashboard_loop())
            await asyncio.sleep(0.05)
            dashboards._refresh_dashboard_with_retry.assert_not_awaited()
            await dashboards.on_family_wars_collected()
            await started
        self.assertEqual(dashboards._refresh_dashboard_with_retry.await_count, len(DASHBOARD_THREADS))

    async def test_a_collector_that_never_reports_does_not_hold_the_dashboards_back_forever(self):
        dashboards = self.dashboards()
        with patch(f"{DASHBOARD}.wait_for_boot_complete", AsyncMock()), patch(f"{DASHBOARD}.FIRST_WAR_CHECK_WAIT_SECONDS", 0.01):
            await dashboards.before_dashboard_loop()
        self.assertFalse(dashboards._first_war_check.is_set())
        self.assertEqual(dashboards._refresh_dashboard_with_retry.await_count, len(DASHBOARD_THREADS))

    async def test_a_later_start_of_the_same_process_is_not_held_back(self):
        dashboards = self.dashboards()
        await dashboards.on_family_wars_collected()
        with patch(f"{DASHBOARD}.wait_for_boot_complete", AsyncMock()), patch(f"{DASHBOARD}.FIRST_WAR_CHECK_WAIT_SECONDS", 30):
            await asyncio.wait_for(dashboards.before_dashboard_loop(), 5)
        self.assertEqual(dashboards._refresh_dashboard_with_retry.await_count, len(DASHBOARD_THREADS))


if __name__ == "__main__":
    unittest.main()
