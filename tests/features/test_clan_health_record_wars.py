"""Clan health's regular wars from synthetic family-record wars; league wars stay on stored history."""

import asyncio
from datetime import datetime
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.reads import WarReads
from elbow_helper.features.clan_health.analysis import ClanHealthAnalyzer
from elbow_helper.features.clan_health.api import ClanHealthCollector
from elbow_helper.features.clan_health.commands.player import ClanHealthPlayerCommandMixin
from elbow_helper.features.clan_health.config import CLAN_ORDER, CWL_CLAN_CODES, UTC
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.clan_health.record_wars import RegularWars
from elbow_helper.features.clan_health.seasons import _season_key_for_datetime

from tests.data.test_war_reads import friendly
from tests.data.test_wars import END, SECOND, START, WarFixture, war

DAY = 86400
CLANS = {"ONE": SimpleNamespace(tag="#PY"), "TWO": SimpleNamespace(tag="#LQ")}
RULES = {"_profile_name": "competitive", "war": {"wars_to_join": 30, "missed_attack_rate_percent": 0}}


def seconds(value):
    return value // SECOND


def moment(value):
    return datetime.fromtimestamp(value, tz=UTC)


class PlayerCommands(ClanHealthPlayerCommandMixin):
    def __init__(self, repository, regular_wars):
        self.repository, self.regular_wars = repository, regular_wars


class RecordRegularWarTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.regular = RegularWars(WarReads(self.store), CLANS)
        self.health = ClanHealthRepository(Path(self.temporary.name) / "clan_health.db")
        await asyncio.to_thread(self.health.initialize)

    async def ended(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.record(friendly(), at=END + 3 * DAY * SECOND)

    async def test_capture_view_counts_an_ongoing_war_at_its_last_check_with_inclusive_seconds(self):
        await self.record(war(), at=self.at)
        start, checked = seconds(START), seconds(self.at)
        activity = await asyncio.to_thread(self.regular.activity, cycle_start_ts=start, cycle_end_ts=checked,
                                           player_tags={"#P0", "#P2"})
        self.assertEqual({tag: (row["wars"], row["attacks_expected"], row["attacks_used"], row["stars"])
                          for tag, row in activity.items()}, {"#P0": (1, 2, 1, 3), "#P2": (1, 2, 0, 0)})
        self.assertEqual(await asyncio.to_thread(self.regular.activity, cycle_start_ts=start,
                                                 cycle_end_ts=checked - 1, player_tags={"#P0"}), {})
        self.assertEqual(await asyncio.to_thread(self.regular.war_counts, cycle_start_ts=start,
                                                 cycle_end_ts=checked, clan_codes={"ONE", "TWO"}), {"ONE": 1})

    async def test_ended_regular_wars_count_once_friendly_wars_never_and_tags_keep_their_spelling(self):
        await self.ended()
        start, end = seconds(START), seconds(END) + 4 * DAY
        activity = await asyncio.to_thread(self.regular.activity, cycle_start_ts=start, cycle_end_ts=end,
                                           player_tags={"#p0", "not a tag", ""})
        self.assertEqual(list(activity), ["#p0"])
        self.assertEqual((activity["#p0"]["wars"], activity["#p0"]["attacks_used"]), (1, 1))
        self.assertEqual(await asyncio.to_thread(self.regular.war_counts, cycle_start_ts=start, cycle_end_ts=end),
                         {"ONE": 1})
        self.assertEqual(await asyncio.to_thread(self.regular.activity, cycle_start_ts=end, cycle_end_ts=start,
                                                 player_tags={"#P0"}), {})

    async def test_long_periods_read_in_pieces_add_up_to_one_read(self):
        await self.record(war(), at=self.at)
        await self.ended()
        start, end = seconds(START) - 3 * DAY, seconds(END) + 4 * DAY

        async def everything():
            return [await asyncio.to_thread(self.regular.activity, cycle_start_ts=start, cycle_end_ts=end,
                                            player_tags={"#P0", "#P2"}),
                    await asyncio.to_thread(self.regular.war_counts, cycle_start_ts=start, cycle_end_ts=end),
                    await asyncio.to_thread(self.regular.attacks, player_tag="#P0", cycle_start_ts=start,
                                            cycle_end_ts=end),
                    await asyncio.to_thread(self.regular.latest_attack_ts, player_tag="#P2", after_ts=start,
                                            until_ts=end)]

        whole = await everything()
        with patch("elbow_helper.features.clan_health.record_wars.SPAN", 7200 * SECOND):
            self.assertEqual(await everything(), whole)
        self.assertEqual((whole[0]["#P0"]["wars"], whole[1], whole[3]), (1, {"ONE": 1}, seconds(END)))

    async def test_health_scores_join_record_regular_wars_with_stored_league_wars(self):
        await self.ended()
        start, end = seconds(START), seconds(END) + 4 * DAY
        await asyncio.to_thread(self.health.store_war_activity, [
            {"war_id": "CWL:#W", "war_type": "CWL", "clan_code": "ONE", "clan_tag": "#PY", "end_ts": start + 10,
             "player_tag": "#P0", "player_name": "Synthetic", "attacks_expected": 1, "attacks_used": 0},
            # The old regular copy of the same war is no longer read.
            {"war_id": "REG:#PY:x:#GR", "war_type": "REG", "clan_code": "ONE", "clan_tag": "#PY",
             "end_ts": seconds(END), "player_tag": "#P0", "player_name": "Synthetic", "attacks_expected": 2,
             "attacks_used": 2},
        ])
        analyzer = ClanHealthAnalyzer(self.health, self.regular)
        rows = [{"player_tag": "#P0", "clan_code": "ONE"}]
        await asyncio.to_thread(analyzer.apply_war_activity, rows, cycle_start=moment(start), cycle_end=moment(end))
        row = rows[0]
        self.assertEqual((row["regular_war_events_joined"], row["regular_war_hits_expected"],
                          row["regular_war_hits_used"], row["regular_war_missed"]), (1, 2, 1, 1))
        self.assertEqual((row["cwl_events_joined"], row["cwl_missed"]), (1, 1))
        self.assertEqual((row["war_events_joined"], row["war_hits_expected"], row["war_missed"],
                          row["war_stars_total"], row["war_attack_count"]), (2, 3, 2, 3.0, 1))
        with patch("elbow_helper.features.clan_health.analysis.flags.effective_player_rules", return_value=RULES):
            await asyncio.to_thread(analyzer.apply_flags, rows, cycle_start=moment(start), cycle_end=moment(end))
        # Five wars would be expected over five days; the clan fought one, so joining it is enough.
        card = rows[0]["signal_cards"]["war_attendance"]
        self.assertEqual((card["verdict"], card["current"]), ("Good", "Joined 1 regular war across the family"))
        self.assertEqual(rows[0]["war_hit_usage_verdict"], "Needs Review")

    async def test_an_unavailable_record_fails_the_report_instead_of_scoring_zero(self):
        broken = RegularWars(SimpleNamespace(health_totals=AsyncMock(side_effect=sqlite3.OperationalError("locked"))),
                             CLANS)
        rows = [{"player_tag": "#P0", "clan_code": "ONE", "war_hits_expected": 4}]
        with self.assertRaises(sqlite3.OperationalError):
            await asyncio.to_thread(ClanHealthAnalyzer(self.health, broken).apply_war_activity, rows,
                                    cycle_start=moment(1), cycle_end=moment(200))
        self.assertEqual(rows, [{"player_tag": "#P0", "clan_code": "ONE", "war_hits_expected": 4}])

    async def test_attack_sheet_rows_come_from_the_record_and_an_unknown_first_hit_stays_unknown(self):
        await self.ended()
        start, end = seconds(START), seconds(END) + 4 * DAY
        (row,) = await asyncio.to_thread(self.regular.attacks, player_tag="#P0", cycle_start_ts=start, cycle_end_ts=end)
        self.assertEqual({key: row[key] for key in (
            "war_type", "clan_code", "end_ts", "war_state", "defender_tag", "defender_map_position",
            "defender_townhall", "stars", "destruction", "fresh_attack", "source")},
            {"war_type": "REG", "clan_code": "ONE", "end_ts": seconds(END), "war_state": "warEnded",
             "defender_tag": "#Q0", "defender_map_position": 1, "defender_townhall": 16, "stars": 3,
             "destruction": 100.0, "fresh_attack": True, "source": "family_record"})
        await asyncio.to_thread(self.health.store_war_attacks, [
            {"war_id": "CWL:#W", "war_type": "CWL", "clan_code": "ONE", "end_ts": seconds(END) - DAY,
             "player_tag": "#P0", "attack_order": 1, "fresh_attack": 1},
            {"war_id": "REG:#OLD", "war_type": "REG", "clan_code": "ONE", "end_ts": seconds(END) + 60,
             "player_tag": "#P0", "attack_order": 1},
        ])
        commands = PlayerCommands(self.health, self.regular)
        merged = await asyncio.to_thread(commands._player_war_attacks, "#P0", moment(start), moment(end))
        self.assertEqual([(r["war_type"], r["source"]) for r in merged], [("REG", "family_record"), ("CWL", "")])
        sheet = commands._build_attack_sheet(merged[:1], "No attacks")
        self.assertEqual(sheet[1][-1], "First hit")
        # The clan's attack count is unknown: no attack can be called a first hit.
        await self.execute("UPDATE war_sides SET attacks=NULL WHERE clan_tag='#PY'")
        (unknown,) = await asyncio.to_thread(self.regular.attacks, player_tag="#P0", cycle_start_ts=start,
                                             cycle_end_ts=end)
        self.assertIsNone(unknown["fresh_attack"])
        self.assertEqual(commands._build_attack_sheet([unknown], "No attacks")[1][-1], "-")
        self.assertEqual(await asyncio.to_thread(self.regular.attacks, player_tag="not a tag",
                                                 cycle_start_ts=start, cycle_end_ts=end), [])

    async def test_the_report_season_follows_a_newer_regular_war_attack_only(self):
        await self.ended()
        latest = await asyncio.to_thread(self.regular.latest_attack_ts, player_tag="#P0", after_ts=0,
                                         until_ts=seconds(END) + 4 * DAY)
        self.assertEqual(latest, seconds(END))
        self.assertIsNone(await asyncio.to_thread(self.regular.latest_attack_ts, player_tag="#P0",
                                                  after_ts=seconds(END) + 1, until_ts=seconds(END) + 4 * DAY))
        commands = PlayerCommands(self.health, self.regular)
        # Nothing stored for the account: the record's attack sets the season.
        self.assertEqual(await asyncio.to_thread(commands._latest_activity_season, "#P0"),
                         _season_key_for_datetime(moment(seconds(END))))
        # A newer stored snapshot wins.
        later = seconds(END) + 40 * DAY
        await asyncio.to_thread(self.health.store_snapshots, later,
                                [{"player_tag": "#P0", "player_name": "Synthetic", "clan_code": "ONE"}])
        self.assertNotEqual(_season_key_for_datetime(moment(later)), _season_key_for_datetime(moment(seconds(END))))
        self.assertEqual(await asyncio.to_thread(commands._latest_activity_season, "#P0"),
                         _season_key_for_datetime(moment(later)))
        self.assertIsNone(await asyncio.to_thread(commands._latest_activity_season, "#P8"))


class NoRegularWarPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_war_collection_asks_only_for_league_wars(self):
        collector = ClanHealthCollector(SimpleNamespace(configured=True), SimpleNamespace(), SimpleNamespace())
        league = AsyncMock(return_value=(1, 2, ["warning"]))
        with (
            patch.object(collector, "_ingest_war_cwl_for_clan", new=league),
            patch.object(collector, "_fetch_coc_json", new=AsyncMock()) as fetch,
        ):
            result = await collector.ingest_family_cwl_activity()
        league_clans = [code for code in CLAN_ORDER if code in CWL_CLAN_CODES]
        self.assertEqual(result, (len(league_clans), 2 * len(league_clans), ["warning"] * len(league_clans)))
        self.assertEqual([call.kwargs["clan_code"] for call in league.await_args_list], league_clans)
        fetch.assert_not_awaited()
        for name in ("_ingest_regular_war_for_clan", "_schedule_war_end_capture", "_run_war_end_capture",
                     "ingest_family_war_activity"):
            self.assertFalse(hasattr(collector, name), name)


if __name__ == "__main__":
    unittest.main()
