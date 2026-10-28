"""Synthetic schedules share destination slots across previews and durable rules."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import random
import threading
import unittest
from unittest.mock import patch

from elbow_helper.agent.actions.contracts import ActionRefused
from elbow_helper.agent.actions.store import AgentActionRepository
from elbow_helper.agent.scheduled.destination_limits import (
    Progression, progressions_conflict, schedules_conflict,
)
from elbow_helper.agent.scheduled.tools import prepare_manage, prepare_save
from agent.discord_actions.management_helpers import fixture

START = datetime(2099, 1, 1, tzinfo=timezone.utc)


def once(offset):
    return {"schedule": {"kind": "once", "at_utc": (START + timedelta(seconds=offset)).isoformat()}}


def interval(seconds, offset=0, **bounds):
    return {"schedule": {"kind": "interval", "seconds": seconds,
                         "anchor_utc": (START + timedelta(seconds=offset)).isoformat(), **bounds}}


class DestinationLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = AgentActionRepository(Path(self.directory.name) / "actions.sqlite3")

    def save(self, rule=None, offset=0, **changes):
        values = dict(kind="reminder", guild_id=1, requester_id=10, destination_channel_id=200,
                      rule={"request": "Synthetic reminder", **(rule or once(offset))},
                      next_at=(START + timedelta(seconds=offset)).timestamp())
        values.update(changes)
        return self.repository.create_standing(**values)

    def test_one_off_rules_share_a_sliding_minute_and_exact_boundary_is_allowed(self):
        self.save()
        for offset in (-59, 0, 30, 59, 59.999999):
            with self.assertRaisesRegex(ActionRefused, "across all your"):
                self.save(offset=offset)
        self.save(offset=60)
        self.assertEqual(len(self.repository.list_standing(requester_id=10)), 2)

    def test_staggered_repeats_and_later_phase_collisions_are_rejected(self):
        with self.assertRaises(ActionRefused):
            self.save(interval(30))
        self.save(interval(120))
        with self.assertRaises(ActionRefused):
            self.save(interval(120, 30), offset=30)
        self.save(interval(120, 60), offset=60)
        other = AgentActionRepository(Path(self.directory.name) / "other.sqlite3")
        self.repository = other
        self.save(interval(86400))
        with self.assertRaises(ActionRefused):
            self.save(interval(86401, 1000), offset=1000)

    def test_dms_share_recipients_across_source_channels_and_rule_kinds(self):
        self.save({**once(0), "deliver_to": "dm", "dm_member_ids": [10, 40]})
        with self.assertRaises(ActionRefused):
            self.save({**once(30), "deliver_to": "dm", "dm_member_ids": [40, 50]},
                      offset=30, destination_channel_id=201)
        with self.assertRaises(ActionRefused):
            self.save({**once(30), "deliver_to": "dm"}, offset=30, kind="request")
        self.save({**once(30), "deliver_to": "dm", "dm_member_ids": [50]}, offset=30)
        self.save(offset=30)  # Channel delivery is a separate destination.
        self.save(offset=30, destination_channel_id=201)
        self.save(offset=30, requester_id=11)
        self.save(offset=30, guild_id=2)

    def test_paused_cancelled_completed_and_replacement_slots(self):
        original = self.save()
        self.assertTrue(self.repository.replace_standing(kind="reminder", identifier=original,
            requester_id=10, rule=once(0), destination_channel_id=200,
            next_at=START.timestamp(), expected_version=0))
        self.repository.set_standing_status(kind="reminder", identifier=original,
                                             requester_id=10, status="paused")
        active = self.save(offset=30)
        with self.assertRaises(ActionRefused):
            self.repository.set_standing_status(kind="reminder", identifier=original,
                                                 requester_id=10, status="active")
        self.repository.set_standing_status(kind="reminder", identifier=active,
                                             requester_id=10, status="cancelled")
        self.assertTrue(self.repository.set_standing_status(kind="reminder", identifier=original,
                                                             requester_id=10, status="active"))
        self.save(offset=120)
        with self.assertRaises(ActionRefused):
            self.repository.replace_standing(kind="reminder", identifier=original,
                requester_id=10, rule=once(90), destination_channel_id=200,
                next_at=(START + timedelta(seconds=90)).timestamp(), expected_version=3)

    def test_finite_bounds_and_completed_runs_do_not_create_future_conflicts(self):
        self.save(interval(60, runs=2))
        self.save(offset=120)
        self.assertFalse(schedules_conflict(interval(60, runs=2), START.timestamp(),
            interval(3600, 180, runs=1), (START + timedelta(seconds=180)).timestamp()))
        ended = interval(60, end_utc=(START + timedelta(seconds=60)).isoformat())
        self.assertFalse(schedules_conflict(ended, START.timestamp(), once(120),
                                            (START + timedelta(seconds=120)).timestamp()))
        finished = {**interval(60, runs=2), "runs_completed": 2}
        self.assertFalse(schedules_conflict(finished, START.timestamp(), once(0), START.timestamp()))

    def test_two_concurrent_saves_cannot_both_claim_the_destination(self):
        barrier = threading.Barrier(2)
        def save():
            barrier.wait()
            try:
                self.save()
                return True
            except ActionRefused:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(save) for _ in range(2)]
            self.assertEqual(sorted(future.result() for future in futures), [False, True])

    def test_progression_solver_matches_finite_enumeration(self):
        rng = random.Random(4)
        for _ in range(500):
            a = Progression(rng.randrange(1000) * 1_000_000,
                            rng.randrange(60, 400) * 1_000_000, rng.randrange(10))
            b = Progression(rng.randrange(1000) * 1_000_000,
                            rng.randrange(60, 400) * 1_000_000, rng.randrange(10))
            expected = any(abs(a.first + i*a.step - b.first - j*b.step) < 60_000_000
                           for i in range(a.last_index + 1) for j in range(b.last_index + 1))
            self.assertEqual(progressions_conflict(a, b), expected)

    def test_calendar_rules_and_interval_phases_include_later_occurrences(self):
        weekly = {"schedule": {"kind": "weekly", "timezone": "UTC", "days": [0],
                                "time": "12:00", "runs": 2}}
        first = datetime(2099, 1, 5, 12, tzinfo=timezone.utc)
        self.assertTrue(schedules_conflict(weekly, first.timestamp(), once(
            (first - START).total_seconds() + 30), (first + timedelta(seconds=30)).timestamp()))
        self.assertFalse(schedules_conflict(weekly, first.timestamp(), once(
            (first - START).total_seconds() + 60), (first + timedelta(seconds=60)).timestamp()))
        monthly = {"schedule": {"kind": "monthly", "timezone": "UTC", "days": [5],
                                 "time": "12:00", "runs": 2}}
        self.assertTrue(schedules_conflict(weekly, first.timestamp(), monthly, first.timestamp()))
        drift = interval(86401, 1000)
        self.assertTrue(schedules_conflict({"schedule": {**weekly["schedule"], "runs": 100000}},
            first.timestamp(), drift, (START + timedelta(seconds=1000)).timestamp()))

    def test_unbounded_calendar_keeps_an_exact_one_minute_gap_from_repeating_messages(self):
        weekly = {"schedule": {"kind": "weekly", "timezone": "UTC", "days": [0],
                                "time": "00:01"}}
        first = datetime(2099, 1, 5, 0, 1, tzinfo=timezone.utc)
        self.assertFalse(schedules_conflict(weekly, first.timestamp(), interval(120), START.timestamp()))


class DestinationPreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = AgentActionRepository(Path(directory.name) / "actions.sqlite3")
        self.context, self.channel, _, _ = fixture()
        self.context.action_repository = self.repository
        handle = patch("elbow_helper.agent.scheduled.tools.check_post_access")
        handle.start()
        self.addCleanup(handle.stop)

    async def prepare(self, offset, **changes):
        values = {"kind": "reminder", "text": "Synthetic reminder", **once(offset), **changes}
        return await prepare_save(self.context, values, registry_factory=lambda: {})

    async def test_conflicting_one_offs_are_refused_before_growing_the_preview(self):
        await self.prepare(0)
        with self.assertRaisesRegex(ActionRefused, "at most one message a minute"):
            await self.prepare(30)
        self.assertEqual(len(self.context.state.proposed_changes), 1)
        await self.prepare(60)
        for action in self.context.state.proposed_changes:
            await action.run()
        self.assertEqual(len(self.repository.list_standing(requester_id=10)), 2)

    async def test_conflicting_new_active_rule_invalidates_preview_and_blocks_run(self):
        await self.prepare(0)
        action = self.context.state.proposed_changes.pop()
        self.repository.create_standing(kind="request", guild_id=1, requester_id=10,
            destination_channel_id=200, rule=once(30), next_at=(START + timedelta(seconds=30)).timestamp())
        self.assertFalse(await action.preview.recheck())
        with self.assertRaises(ActionRefused):
            await action.run()

    async def test_resume_checks_saved_and_pending_rules_before_preview(self):
        await self.prepare(0)
        await self.context.state.proposed_changes.pop().run()
        identity = self.repository.list_standing(requester_id=10)[0]["request_id"]
        self.repository.set_standing_status(kind="reminder", identifier=identity,
                                            requester_id=10, status="paused")
        await self.prepare(30)
        with self.assertRaises(ActionRefused):
            await prepare_manage(self.context, {"kind": "reminder", "id": identity,
                                                "operation": "resume"})
        self.assertEqual(len(self.context.state.proposed_changes), 1)
