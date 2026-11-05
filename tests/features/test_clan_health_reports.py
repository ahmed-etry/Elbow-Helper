"""Clan health reports worked out from the family record when asked for, and its own settings file (synthetic)."""

from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.clan_health.analysis import ClanHealthAnalyzer
from elbow_helper.features.clan_health.commands.clan import ClanHealthClanCommandMixin
from elbow_helper.features.clan_health.commands.player import ClanHealthPlayerCommandMixin
from elbow_helper.features.clan_health.config import CLAN_EXPORT_ORDER, UTC
from elbow_helper.features.clan_health.database import schema as settings
from elbow_helper.features.clan_health.database import config_store
from elbow_helper.features.clan_health.record import HealthRecord

from tests.data.legacy_health_schema import LegacyHealthFile

RULES = {"_profile_name": "casual", "war": {"wars_to_join": 0, "missed_attack_rate_percent": 0},
         "raids": {"minimum_capital_gold_per_event": 0}, "clan_games": {"minimum_points_per_event": 0}}
NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
CODE = CLAN_EXPORT_ORDER[0]


class Wars:
    """No wars at all, as the record's war reads would say."""

    def activity(self, **window):
        return {}

    def war_counts(self, **window):
        return {}

    def attacks(self, **window):
        return []


class RecordStub:
    """The record as clan health reads it: one member in one clan, with progress, donations and one raid."""

    def __init__(self, *, members=None, stays=None, known=True, progress=True):
        self.members = {CODE: [{"player_tag": "#PP29L", "player_name": "Synthetic Main"}]} if members is None else members
        self.stay_rows = stays
        self.known, self.with_progress = known, progress
        self.calls = []

    def roster(self, codes, *, at_ts):
        self.calls.append(("roster", tuple(codes), at_ts))
        return at_ts - 600, {code: self.members.get(code, []) for code in codes}

    def progress(self, tags, *, at_ts, until_ts=None):
        if not self.with_progress:
            return {}
        later = at_ts >= int(NOW.timestamp()) - 60
        return {tag: {"townhall": 15, "hero_sum": 200 if later else 190, "pet_sum": 30, "equipment_sum": 40,
                      "troop_sum": 300, "spell_sum": 50, "games_total": 9000 if later else 5000,
                      "capital_contrib": 1000} for tag in tags}

    def donations(self, tags, **window):
        return {tag: {"donations": 120, "donations_received": 30, "samples": 3} for tag in tags}

    def raid_activity(self, *, player_tags, **window):
        return {tag: {"attacks": 6, "loot": 4000, "raid_weekends_participated": 1} for tag in player_tags}, 2

    def raid_lines(self, *, player_tags=None, **window):
        return ([{"player_tag": "#PP29L", "clan_code": CODE, "weekend_id": "w1", "attacks": 6, "loot": 4000,
                  "end_ts": 1}] if self.known else []), 2

    def stays(self, tag, *, cycle_start_ts, cycle_end_ts):
        self.calls.append(("stays", cycle_start_ts, cycle_end_ts))
        if self.stay_rows is not None:
            return self.stay_rows(cycle_start_ts, cycle_end_ts)
        return [{"clan_code": CODE, "start_ts": cycle_start_ts, "end_ts": cycle_end_ts, "ongoing": True}] if self.known else []

    def directory_players(self, tags):
        return {tag: {"player_tag": tag, "player_name": "Synthetic Main", "clan_code": CODE} for tag in tags} if (
            self.known) else {}


class Reports(ClanHealthClanCommandMixin, ClanHealthPlayerCommandMixin):
    def __init__(self, record):
        self.record = record
        self.regular_wars = self.league_wars = Wars()
        self.analyzer = ClanHealthAnalyzer(record, self.regular_wars, self.league_wars)
        # Reports neither ask Clash nor need it set up: any use of the client fails the test.
        self.clash_client = SimpleNamespace(configured=False, get=AsyncMock(side_effect=AssertionError("Clash asked")))
        self.prepared = []

    async def prepare_health_export(self, **workbook):
        self.prepared.append(workbook)
        return SimpleNamespace(**workbook)


class ClanReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for target in ("flags", "overlays"):
            patcher = patch(f"elbow_helper.features.clan_health.analysis.{target}.effective_player_rules",
                            return_value=RULES)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_the_roster_comes_from_the_record_at_the_period_end_and_verdicts_are_worked_out(self):
        record = RecordStub()
        reports = Reports(record)
        result = await reports.run_clan_health_export(CODE, mode="custom", date_from="2026-10-01", date_to="2026-10-15")
        self.assertEqual(result.status, "complete")
        end = int(datetime(2026, 10, 15, tzinfo=UTC).timestamp())
        self.assertEqual(record.calls[0], ("roster", (CODE,), end))
        workbook = reports.prepared[0]
        self.assertIn("Notes: Roster from 2026-10-14 23:50 UTC.", workbook["summary_lines"])
        activity = dict(workbook["sheets"])["Activity"]
        self.assertEqual(activity[1][0], "Synthetic Main")
        # Raid weekends 1 of 2, clan games and donations in the window, progression against the start.
        self.assertEqual(activity[1][3:9], ["1/2", 6, 4000, 0, 120, 30])
        reports.clash_client.get.assert_not_awaited()

    async def test_the_report_needs_no_clash_connection_and_the_cog_takes_no_client(self):
        import inspect
        from elbow_helper.features.clan_health.cog import ClanHealth
        reports = Reports(RecordStub())
        reports.clash_client = SimpleNamespace(configured=False)
        self.assertEqual((await reports.run_clan_health_export(CODE)).status, "complete")
        self.assertNotIn("clash_client", inspect.signature(ClanHealth.__init__).parameters)
        self.assertFalse(hasattr(ClanHealthClanCommandMixin, "clash_client"))
        del reports.clash_client
        self.assertEqual((await reports.run_clan_health_export(CODE)).status, "complete")

    async def test_a_clan_without_members_at_the_end_has_no_report(self):
        reports = Reports(RecordStub(members={}))
        result = await reports.run_clan_health_export(CODE)
        self.assertEqual(result.status, "empty")
        self.assertIn("No clan roster is available near the end of that period.", result.issue)
        self.assertEqual(reports.prepared, [])


class PlayerReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for target in ("flags", "overlays"):
            patcher = patch(f"elbow_helper.features.clan_health.analysis.{target}.effective_player_rules",
                            return_value=RULES)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def export(self, record):
        reports = Reports(record)
        start = NOW - timedelta(days=30)
        result = await reports.build_player_health_export(
            player_tag="#PP29L", now=NOW, window_mode="last_30d", season_key="last 30d", trend_season_key="2026-10",
            cycle_start=start, cycle_end=NOW, window_label="Last 30 days")
        reports.clash_client.get.assert_not_awaited()
        return result

    async def test_an_account_the_record_does_not_know_has_no_report(self):
        self.assertIsNone(await self.export(RecordStub(known=False)))

    async def test_report_reads_the_record_only_and_the_trend_covers_two_completed_seasons(self):
        record = RecordStub()
        result = await self.export(record)
        sheets = dict(result.sheets)
        self.assertEqual(sheets["Overview"][1], ["Player", "Synthetic Main", "#PP29L"])
        self.assertEqual(sheets["Overview"][5][1:], ["No change", "Compared with the previous two completed periods."])
        self.assertEqual(sheets["Clan History"][1][0], CODE)
        # Window, then the seasons ending 11 October and 11 September.
        season_ends = [call[2] for call in record.calls if call[0] == "stays"][1:]
        self.assertEqual(season_ends, [int(datetime(2026, 10, 11, 8, tzinfo=UTC).timestamp()),
                                       int(datetime(2026, 9, 11, 8, tzinfo=UTC).timestamp())])
        self.assertNotIn("Some player data was unavailable; showing partial results.", result.summary_lines)

    async def test_seasons_outside_the_family_do_not_count_and_missing_progress_is_partial(self):
        start = int((NOW - timedelta(days=30)).timestamp())
        record = RecordStub(progress=False, stays=lambda after, before: (
            [{"clan_code": CODE, "start_ts": after, "end_ts": before, "ongoing": True}] if after >= start else []))
        result = await self.export(record)
        overview = dict(result.sheets)["Overview"]
        self.assertEqual(overview[5][1], "Not enough history")
        self.assertEqual(overview[-1], ["Data note", "Partial results",
                                        "Some player data was unavailable; showing partial results."])


class RecordAdapterTests(unittest.TestCase):
    def test_windows_include_both_ends_in_whole_seconds_and_keep_the_callers_spelling(self):
        seen = {}

        async def progress(tags, *, as_of, until=None):
            seen["progress"] = (tags, as_of, until)
            return {"rows": [{"player_tag": tag, "known": True} for tag in tags]}

        async def donations(tags, *, after, before):
            seen["donations"] = (after, before)
            return {"rows": [{"player_tag": tag} for tag in tags]}

        async def ended(*, after, before):
            seen["raids"] = (after, before)
            return {"weekends": [{"clan_tag": "#PP9", "start_at": 5, "end_at": 9_000_000,
                                  "members": [{"player_tag": "#PP29L", "attacks": 6, "capital_loot": 10}]}],
                    "weekend_ends": [9_000_000]}

        record = HealthRecord(SimpleNamespace(progress=progress, donations=donations), SimpleNamespace(ended=ended),
                              SimpleNamespace(), clans={"ONE": SimpleNamespace(tag="#PP9")})
        self.assertEqual(list(record.progress(["#pp29l", "bad tag"], at_ts=10, until_ts=20)), ["#pp29l"])
        self.assertEqual(seen["progress"], (["#PP29L"], 10_999_999, 20_999_999))
        record.donations(["#PP29L"], cycle_start_ts=10, cycle_end_ts=20)
        self.assertEqual(seen["donations"], (10_999_999, 21_000_000))
        totals, weekends = record.raid_activity(cycle_start_ts=1, cycle_end_ts=20, player_tags=["#pp29l"])
        self.assertEqual((totals, weekends, seen["raids"]),
                         ({"#pp29l": {"attacks": 6, "loot": 10, "raid_weekends_participated": 1}}, 1,
                          (1_000_000, 21_000_000)))


    def test_a_copied_stay_and_its_record_continuation_show_as_one_clan_stay_inside_the_window(self):
        def stay(clan, from_at, to_at, last, basis):
            return {"clan_tag": clan, "clan_code": None, "from_at": from_at, "to_at": to_at,
                    "first_seen_at": from_at, "last_seen_at": last, "basis": basis}

        async def stays(tags, *, after, before, as_of):
            return {"rows": [{"player_tag": tags[0], "stays": [
                stay("#PP9", 0, 50_000_000, 50_000_000, "observed_hourly"),
                stay("#PP9", 50_000_000, 70_000_000, 69_000_000, "record"),
                stay("#PP8", 70_000_000, None, 95_000_000, "record")]}]}

        record = HealthRecord(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(stays=stays),
                              clans={"ONE": SimpleNamespace(tag="#PP9"), "TWO": SimpleNamespace(tag="#PP8")})
        self.assertEqual([(s["clan_code"], s["start_ts"], s["end_ts"], s["ongoing"])
                          for s in record.stays("#PP29L", cycle_start_ts=10, cycle_end_ts=90)],
                         [("ONE", 10, 69, False), ("TWO", 70, 90, True)])


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path, self.old = self.root / "settings.sqlite3", self.root / "clan_health.db"

    def old_file(self):
        LegacyHealthFile(self.old).initialize()
        with closing(sqlite3.connect(self.old)) as conn, conn:
            conn.execute("INSERT INTO clan_player_health_config VALUES ('BEH','competitive','a','b','{}')")
            conn.executemany("INSERT INTO clan_health_config_audit VALUES (?,?,'BEH','player','war','k',?,?,7,'Lead')",
                             [(4, "2026-04-20T00:00:00+00:00", "1", "2"), (9, "2026-04-21T00:00:00+00:00", None, "3")])

    def table(self, name):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()

    def test_settings_and_change_log_copy_once_with_a_receipt_and_the_old_file_is_left_alone(self):
        self.old_file()
        before = self.old.read_bytes()
        result = settings.copy_old_settings(self.path, self.old)
        self.assertEqual((result["outcome"], result["settings"], result["audit"]), ("copied", 1, 2))
        self.assertEqual([row[0] for row in self.table("clan_health_config_audit")], [4, 9])
        self.assertEqual(self.table("clan_player_health_config"), [("BEH", "competitive", "a", "b", "{}")])
        self.assertEqual(self.table("old_file_copy")[0][2:5], ("copied", 1, 2))
        self.assertEqual(self.old.read_bytes(), before)
        # A later start never opens the old file again.
        with patch.object(settings, "_old_rows", side_effect=AssertionError("old file opened")):
            self.assertIsNone(settings.copy_old_settings(self.path, self.old))

    def test_without_an_old_file_the_receipt_says_so_and_settings_still_save(self):
        result = settings.copy_old_settings(self.path, self.root / "missing.db")
        self.assertEqual((result["outcome"], result["settings"]), ("no_old_file", 0))
        with patch.object(config_store, "DB_PATH", self.path):
            config_store.invalidate_cache()
            config_store.seed_missing_configs()
            self.assertIsNotNone(config_store.get_player_config("BEH"))
        config_store.invalidate_cache()
        self.assertGreater(len(self.table("clan_player_health_config")), 0)

    def test_a_failed_copy_keeps_nothing_and_writes_no_receipt(self):
        self.old_file()
        with patch.object(settings, "_old_rows", return_value=([("BEH", "t", "a", "b", "{}")] * 2, [])):
            with self.assertRaises(sqlite3.IntegrityError):
                settings.copy_old_settings(self.path, self.old)
        self.assertEqual((self.table("clan_player_health_config"), self.table("old_file_copy")), ([], []))
        self.assertEqual(settings.copy_old_settings(self.path, self.old)["settings"], 1)


class CogStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_copies_settings_then_seeds_and_starts_no_loop(self):
        from elbow_helper.features.clan_health.cog import ClanHealth
        events = []
        with patch("elbow_helper.features.clan_health.database.schema.copy_old_settings",
                   side_effect=lambda *paths: events.append("copy")), \
                patch("elbow_helper.features.clan_health.database.config_store.seed_missing_configs",
                      side_effect=lambda: events.append("seed")):
            await ClanHealth.cog_load(SimpleNamespace())
        self.assertEqual(events, ["copy", "seed"])
        self.assertFalse(hasattr(ClanHealth, "_snapshot_log_loop"))


if __name__ == "__main__":
    unittest.main()
