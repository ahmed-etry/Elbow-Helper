import gc
from pathlib import Path
import sqlite3
import tempfile
import unittest

from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.clan_health.queries import ClanHealthQueries


def _row(tag, *, name="Player", clan="BEH", status="Good", missed=0, donations=10):
    return {
        "clan_code": clan, "player_tag": tag, "player_name": name,
        "status": status, "flags": ["missed"] if missed else [], "note": "",
        "war_hits_used": 2 - missed, "war_hits_expected": 2,
        "war_missed": missed, "donations": donations,
    }


class ClanHealthQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = ClanHealthRepository(Path(self.temporary.name) / "health.sqlite3")
        self.repository.initialize()
        self.queries = ClanHealthQueries(self.repository)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.repository = None
        self.queries = None
        gc.collect()
        self.temporary.cleanup()

    def store(self, run_id, *, created, start, end, rows, partial=False, scope="BACKGROUND_ALL"):
        self.repository.store_report(
            run_id=run_id, created_ts=created, season_key=f"season-{end}",
            scope=scope, partial=partial, cycle_start_ts=start,
            cycle_end_ts=end, rows=rows,
        )

    async def test_player_directory_search_filters_last_seen_clan_before_paging(self):
        self.repository.store_snapshots(100, [
            {"player_tag": "#P0", "player_name": "Alice", "clan_code": "BEH"},
            {"player_tag": "#P2", "player_name": "Alice Two", "clan_code": "BEH"},
            {"player_tag": "#P8", "player_name": "Alice Three", "clan_code": "BEH"},
        ])
        self.repository.store_snapshots(200, [
            {"player_tag": "#P0", "player_name": "Alice", "clan_code": "BEC"},
        ])

        first = await self.queries.search_players(
            "ali", limit=1, clan_code="BEH",
        )
        second = await self.queries.search_players(
            "ali", limit=1, offset=1, clan_code="BEH",
        )
        self.assertEqual([row["player_tag"] for row in (*first, *second)],
                         ["#P8", "#P2"])
        self.assertTrue(all(row["last_seen_ts"] == 100 for row in (*first, *second)))
        self.assertEqual((await self.queries.search_players(
            "ali", limit=1,
        ))[0]["player_tag"], "#P0")
        with self.assertRaises(ValueError):
            await self.queries.search_players("ali", clan_code="UNKNOWN")

    async def test_run_history_uses_latest_complete_run_per_period_and_pages(self):
        self.store("old-original", created=141, start=100, end=140,
                   rows=[_row("#P0"), _row("#P2")])
        self.store("old-revised", created=142, start=100, end=140,
                   rows=[_row("#P0")])
        self.store("new", created=241, start=200, end=240,
                   rows=[_row("#P0"), _row("#P8")])
        self.store("partial", created=341, start=300, end=340,
                   rows=[_row("#P0")], partial=True)
        self.store("player", created=441, start=400, end=440,
                   rows=[_row("#P0")], scope="PLAYER")

        runs = await self.queries.report_runs("BEH", limit=10)
        self.assertEqual([run.run_id for run in runs], ["new", "old-revised"])
        self.assertEqual([run.player_count for run in runs], [2, 1])
        previous = await self.queries.report_runs(
            "BEH", before_run_id=runs[0].run_id, limit=1,
        )
        self.assertEqual([run.run_id for run in previous], ["old-revised"])
        with self.assertRaises(ValueError):
            await self.queries.report_runs("BEH", before_run_id="old-original")

    async def test_exact_report_is_typed_complete_and_clan_scoped(self):
        self.store("mixed", created=141, start=100, end=140, rows=[
            _row("#P0", name="Alpha", missed=1),
            _row("#P2", name="Beta", clan="BEC"),
        ])
        snapshot = await self.queries.report("BEH", "mixed")
        self.assertEqual(snapshot.run.player_count, 1)
        self.assertEqual(snapshot.rows[0].player_tag, "#P0")
        self.assertEqual(snapshot.rows[0].war_missed, 1)
        self.assertEqual(snapshot.rows[0].flags, ("missed",))
        self.assertIsNone(await self.queries.report("BE4", "mixed"))

    async def test_malformed_flags_are_visible_as_snapshot_issues(self):
        self.store("report", created=141, start=100, end=140, rows=[_row("#P0")])
        with sqlite3.connect(self.repository.path) as connection, connection:
            connection.execute(
                "UPDATE report_players SET flags_json = ? WHERE run_id = ?",
                ("not-json", "report"),
            )
        snapshot = await self.queries.report("BEH", "report")
        self.assertEqual(snapshot.rows[0].flags, ())
        self.assertEqual(snapshot.issues, ("invalid_flags:#P0",))

    async def test_invalid_clan_and_cursor_fail_closed(self):
        with self.assertRaises(ValueError):
            await self.queries.report_runs("NOPE")
        with self.assertRaises(ValueError):
            await self.queries.report_runs("BEH", before_run_id="")

    async def test_family_movement_history_uses_consecutive_complete_runs_and_pages(self):
        self.store("r1", created=100, start=1, end=90, rows=[
            _row("#P0", name="Alpha", clan="BEH"),
        ])
        self.store("r2", created=200, start=91, end=190, rows=[
            _row("#P0", name="Alpha", clan="BEC"),
            _row("#P2", name="Beta", clan="BEH"),
        ])
        self.store("r3", created=300, start=191, end=290, rows=[
            _row("#P0", name="Alpha", clan="BEC"),
        ])
        self.store("r4", created=400, start=291, end=390, rows=[
            _row("#P0", name="Alpha", clan="BE4"),
            _row("#P2", name="Beta", clan="BEH"),
        ])
        self.store("partial", created=500, start=391, end=490, partial=True, rows=[
            _row("#P0", name="Alpha", clan="BEH"),
        ])

        first = await self.queries.family_movement_history(interval_limit=2)
        self.assertEqual([run.run_id for run in first.runs], ["r4", "r3", "r2"])
        self.assertEqual(first.next_before_run_id, "r2")
        self.assertEqual(
            [row.transition for row in first.movements],
            ["observed_entered_family", "observed_family_clan_change",
             "observed_left_family"],
        )
        self.assertEqual(first.intervals[0].movement_count, 2)
        self.assertEqual(first.intervals[1].movement_count, 1)

        second = await self.queries.family_movement_history(
            interval_limit=2, before_run_id=first.next_before_run_id,
        )
        self.assertEqual([run.run_id for run in second.runs], ["r2", "r1"])
        self.assertIsNone(second.next_before_run_id)
        self.assertEqual(len(second.movements), 2)
        with self.assertRaises(ValueError):
            await self.queries.family_movement_history(before_run_id="partial")

    async def test_family_movement_history_excludes_ambiguous_same_run_account(self):
        self.store("old", created=100, start=1, end=90, rows=[
            _row("#P0", name="Alpha", clan="BEH"),
        ])
        self.store("new", created=200, start=91, end=190, rows=[
            _row("#P0", name="Alpha", clan="BEC"),
            _row("P0", name="Alpha", clan="BEH"),
        ])

        history = await self.queries.family_movement_history()
        self.assertEqual(history.movements, ())
        self.assertEqual(history.runs[0].ambiguous_account_tags, ("#P0",))
        self.assertEqual(
            history.intervals[0].excluded_ambiguous_account_tags, ("#P0",),
        )


if __name__ == "__main__":
    unittest.main()
