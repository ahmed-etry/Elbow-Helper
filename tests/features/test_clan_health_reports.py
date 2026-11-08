"""Clan health reports worked out from the family record when asked for, and its own settings file (synthetic)."""

from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.clan_health.analysis import ClanHealthAnalyzer
from elbow_helper.features.clan_health.commands.clan import ClanHealthClanCommandMixin
from elbow_helper.features.clan_health.commands.player import ClanHealthPlayerCommandMixin
from elbow_helper.features.clan_health.config import CLAN_EXPORT_ORDER, UTC
from elbow_helper.features.clan_health.record import HealthRecord


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


if __name__ == "__main__":
    unittest.main()
