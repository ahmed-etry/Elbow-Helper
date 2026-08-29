from __future__ import annotations

import gc
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from elbow_helper.features.clan_health.api import _validated_clan_members
from elbow_helper.features.clan_health.config import CLAN_ORDER
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.clan_health.snapshots import ClanHealthSnapshotMixin


def _member(tag: str) -> dict[str, object]:
    return {"tag": tag, "name": tag}


def _report_row(tag: str, *, clan_code: str = "BEH") -> dict[str, object]:
    return {
        "clan_code": clan_code,
        "player_tag": tag,
        "player_name": tag,
    }


class ClanRosterPayloadTests(unittest.TestCase):
    def test_declared_member_count_must_match_the_member_list(self) -> None:
        members, declared, complete = _validated_clan_members(
            {
                "members": 50,
                "memberList": [_member("#A"), _member("#B")],
            }
        )

        self.assertEqual(len(members), 2)
        self.assertEqual(declared, 50)
        self.assertFalse(complete)

    def test_complete_member_list_is_accepted(self) -> None:
        members, declared, complete = _validated_clan_members(
            {
                "members": 2,
                "memberList": [_member("#A"), _member("#B")],
            }
        )

        self.assertEqual(len(members), 2)
        self.assertEqual(declared, 2)
        self.assertTrue(complete)


class _SnapshotHarness(ClanHealthSnapshotMixin):
    def __init__(self, *, incomplete_clan: str | None):
        self.clash_client = SimpleNamespace(configured=True)

        async def collect_clan_live(*, clan_code, cycle_start, cycle_end):
            del cycle_start, cycle_end
            return {
                "clan_code": clan_code,
                "players": [_report_row(f"#{clan_code}", clan_code=clan_code)],
                "roster_complete": clan_code != incomplete_clan,
            }, []

        self.collector = SimpleNamespace(
            collect_clan_live=AsyncMock(side_effect=collect_clan_live),
            ingest_family_war_activity=AsyncMock(return_value=(0, 0, [])),
        )
        self.analyzer = SimpleNamespace(
            apply_war_activity=MagicMock(),
            apply_raid_activity=MagicMock(),
            apply_donation_activity=MagicMock(),
            apply_progression_fallback=MagicMock(),
            apply_flags=MagicMock(),
        )
        self.repository = SimpleNamespace(
            store_snapshots=MagicMock(),
            store_report=MagicMock(),
        )
        self._last_war_ingest_ts = int(time.time())
        self._last_snapshot_ts = 0
        self._last_startup_sync_ts = 0
        self._last_background_log_sig = None
        self._last_background_log_ts = 0

    @staticmethod
    def _warning_preview(warnings, *, limit):
        return " | ".join(warnings[:limit])


class BackgroundSnapshotCompletenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_clan_marks_the_family_run_partial(self) -> None:
        harness = _SnapshotHarness(incomplete_clan=CLAN_ORDER[0])

        await harness._run_background_snapshot_cycle(trigger="loop")

        self.assertTrue(harness.repository.store_report.call_args.kwargs["partial"])

    async def test_complete_clans_keep_the_family_run_complete(self) -> None:
        harness = _SnapshotHarness(incomplete_clan=None)

        await harness._run_background_snapshot_cycle(trigger="loop")

        self.assertFalse(harness.repository.store_report.call_args.kwargs["partial"])


class CompleteReportSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = ClanHealthRepository(
            Path(self.temporary.name) / "clan-health.sqlite3"
        )
        self.repository.initialize()
        self.addCleanup(self._cleanup_database)

    def _cleanup_database(self) -> None:
        self.repository = None
        gc.collect()
        self.temporary.cleanup()

    def _store_report(
        self,
        *,
        run_id: str,
        created_ts: int,
        scope: str,
        partial: bool,
        rows: list[dict[str, object]],
    ) -> None:
        self.repository.store_report(
            run_id=run_id,
            created_ts=created_ts,
            season_key="rolling",
            scope=scope,
            partial=partial,
            cycle_start_ts=1,
            cycle_end_ts=created_ts,
            rows=rows,
        )

    def test_partial_and_player_runs_do_not_replace_complete_roster(self) -> None:
        self._store_report(
            run_id="complete-50",
            created_ts=100,
            scope="BACKGROUND_ALL",
            partial=False,
            rows=[_report_row("#A"), _report_row("#B")],
        )
        self._store_report(
            run_id="partial-12",
            created_ts=200,
            scope="BACKGROUND_ALL",
            partial=True,
            rows=[_report_row("#A")],
        )
        self._store_report(
            run_id="single-player",
            created_ts=300,
            scope="PLAYER",
            partial=False,
            rows=[_report_row("#A")],
        )

        run, rows = self.repository.latest_report_before(
            cycle_end_ts=400,
            selected_clans=["BEH"],
        )

        self.assertEqual(run["run_id"], "complete-50")
        self.assertEqual({row["player_tag"] for row in rows}, {"#A", "#B"})

    def test_new_complete_roster_can_replace_the_previous_one(self) -> None:
        self._store_report(
            run_id="complete-50",
            created_ts=100,
            scope="BACKGROUND_ALL",
            partial=False,
            rows=[_report_row("#A"), _report_row("#B")],
        )
        self._store_report(
            run_id="complete-49",
            created_ts=200,
            scope="BACKGROUND_ALL",
            partial=False,
            rows=[_report_row("#A")],
        )

        run, rows = self.repository.latest_report_before(
            cycle_end_ts=300,
            selected_clans=["BEH"],
        )

        self.assertEqual(run["run_id"], "complete-49")
        self.assertEqual([row["player_tag"] for row in rows], ["#A"])
