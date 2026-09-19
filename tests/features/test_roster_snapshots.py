from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from elbow_helper.features.rosters.repository import RosterRepository
from elbow_helper.features.rosters.services.queries import RosterQueries


class RosterSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = RosterRepository(Path(self.directory.name) / "rosters.sqlite")
        self.queries = RosterQueries(self.repository)
        self.roster = self.repository.create_roster(
            guild_id=1, name="CWL", clan_code="BEC", role_id=None, max_members=50,
        )

    def add_account(self, roster, tag, member_id=42):
        self.repository.add_members(roster.id, roster.active_cycle_id, member_id, [
            {"player_tag": tag, "player_name": tag, "townhall": 16},
        ], 50)

    async def test_cycle_pages_do_not_shift_when_new_cycles_are_added(self):
        ids = [self.repository.start_cycle(self.roster.id, str(index))[0].active_cycle_id
               for index in range(5)]
        first = await self.queries.cycles(1, self.roster.id, limit=2)
        self.repository.start_cycle(self.roster.id, "new")
        seen = [cycle.id for cycle in first.cycles]
        page = first
        while page.next_before_id is not None:
            page = await self.queries.cycles(1, self.roster.id, before_id=page.next_before_id, limit=2)
            seen.extend(cycle.id for cycle in page.cycles)
        self.assertEqual(seen, list(reversed(ids)))

    async def test_cycle_directory_distinguishes_missing_roster_from_no_cycles(self):
        self.assertIsNone(await self.queries.cycles(2, self.roster.id))
        self.assertIsNone(await self.queries.cycles(1, 999))
        page = await self.queries.cycles(1, self.roster.id)
        self.assertEqual(page.cycles, ())
        self.assertIsNone(page.next_before_id)

    async def test_cycle_page_bounds_are_enforced(self):
        for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True},
                       {"before_id": 0}, {"before_id": -1}, {"before_id": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    await self.queries.cycles(1, self.roster.id, **kwargs)

    async def test_snapshot_only_includes_its_own_posts(self):
        other = self.repository.create_roster(
            guild_id=1, name="Other", clan_code="BEH", role_id=None, max_members=50,
        )
        self.repository.add_post(other.id, 200, 201)
        self.repository.add_post(self.roster.id, 100, 101)
        snapshot = await self.queries.snapshot(1, self.roster.id)
        self.assertEqual([(post.channel_id, post.message_id) for post in snapshot.posts], [(100, 101)])

    async def test_missing_and_foreign_guild_rosters_are_not_returned(self):
        self.assertIsNone(await self.queries.snapshot(2, self.roster.id))
        self.assertIsNone(await self.queries.snapshot(1, 999))

    async def test_unopened_roster_is_distinct_from_an_empty_cycle(self):
        unopened = await self.queries.snapshot(1, self.roster.id)
        self.assertIsNone(unopened.cycle)
        self.assertEqual(unopened.members, ())
        roster, _ = self.repository.start_cycle(self.roster.id, "2026-09")
        opened = await self.queries.snapshot(1, self.roster.id)
        self.assertEqual(opened.cycle.id, roster.active_cycle_id)
        self.assertEqual(opened.members, ())

    async def test_historical_cycle_never_returns_current_cycle_members(self):
        previous, _ = self.repository.start_cycle(self.roster.id, "2026-08")
        self.add_account(previous, "#2PP")
        self.repository.close_cycle(self.roster.id)
        current, _ = self.repository.start_cycle(self.roster.id, "2026-09")
        self.add_account(current, "#2PQ")
        historic = await self.queries.snapshot(1, self.roster.id, cycle_id=previous.active_cycle_id)
        active = await self.queries.snapshot(1, self.roster.id)
        self.assertEqual([item.player_tag for item in historic.members], ["#2PP"])
        self.assertEqual([item.player_tag for item in active.members], ["#2PQ"])
        self.assertEqual(historic.cycle.cycle_key, "2026-08")
        self.assertIsNotNone(historic.cycle.closed_ts)
        self.assertGreater(active.observed_ts, 0)

    async def test_missing_and_foreign_cycles_do_not_fall_back(self):
        other = self.repository.create_roster(
            guild_id=1, name="Other", clan_code="BEH", role_id=None, max_members=50,
        )
        other, _ = self.repository.start_cycle(other.id, "2026-09")
        for cycle_id in (999, other.active_cycle_id):
            with self.subTest(cycle_id=cycle_id):
                with self.assertRaises(KeyError):
                    await self.queries.snapshot(1, self.roster.id, cycle_id=cycle_id)

    async def test_cycle_change_during_read_cannot_mix_snapshots(self):
        previous, _ = self.repository.start_cycle(self.roster.id, "2026-08")
        self.add_account(previous, "#2PP")
        original_connect = self.repository.connect
        # A separate owner instance performs a concurrent committed write.
        writer = RosterRepository(self.repository.path)
        changed = []

        @contextmanager
        def connect():
            with original_connect() as connection:
                def trace(sql):
                    if "SELECT * FROM roster_cycles" in sql and not changed:
                        changed.append(True)
                        writer.start_cycle(self.roster.id, "2026-09")
                connection.set_trace_callback(trace)
                yield connection

        with patch.object(self.repository, "connect", connect):
            snapshot = await self.queries.snapshot(1, self.roster.id)
        self.assertEqual(changed, [True])
        self.assertEqual(snapshot.roster.active_cycle_id, previous.active_cycle_id)
        self.assertEqual(snapshot.cycle.id, previous.active_cycle_id)
        self.assertEqual([item.player_tag for item in snapshot.members], ["#2PP"])
        self.assertNotEqual(self.repository.get_roster(self.roster.id).active_cycle_id, previous.active_cycle_id)
