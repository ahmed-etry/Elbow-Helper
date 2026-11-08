"""Clan health's regular and league wars from synthetic family-record wars."""

import asyncio
from datetime import datetime
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.reads import WarReads
from elbow_helper.features.clan_health.analysis import ClanHealthAnalyzer
from elbow_helper.features.clan_health.commands.player import ClanHealthPlayerCommandMixin
from elbow_helper.features.clan_health.config import UTC
from elbow_helper.features.clan_health.record_wars import RegularWars

from tests.data.test_war_reads import friendly
from tests.data.test_wars import END, SECOND, START, WarFixture, group, war

DAY = 86400
CLANS = {"ONE": SimpleNamespace(tag="#PY"), "TWO": SimpleNamespace(tag="#LQ")}
RULES = {"_profile_name": "competitive", "war": {"wars_to_join": 30, "missed_attack_rate_percent": 0}}


def seconds(value):
    return value // SECOND


def moment(value):
    return datetime.fromtimestamp(value, tz=UTC)


def clash(at):
    return moment(at // SECOND).strftime("%Y%m%dT%H%M%S.000Z")


class PlayerCommands(ClanHealthPlayerCommandMixin):
    def __init__(self, repository, regular_wars, league_wars):
        self.repository, self.regular_wars, self.league_wars = repository, regular_wars, league_wars


class RecordRegularWarTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.regular = RegularWars(WarReads(self.store), CLANS)
        self.league = RegularWars(WarReads(self.store), CLANS, types=("CWL",))
        self.health = SimpleNamespace()

    async def ended(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.record(friendly(), at=END + 3 * DAY * SECOND)

    async def league_round(self, *, at=START + 10 * SECOND, missed=True, war_tag="#R0"):
        """One ended league war of #PY on the window's first day, the collector's way; #P0 attacks unless ``missed``."""
        payload = war("warEnded", clans=("#PY", "#CU"), end=clash(at))
        payload.update(attacksPerMember=1, preparationStartTime=clash(at - 2 * DAY * SECOND),
                       startTime=clash(at - DAY * SECOND))
        if missed:
            first = payload["clan"]["members"][0]
            first.pop("attacks")
            payload["clan"]["attacks"] -= 1
            payload["opponent"]["members"][0].pop("bestOpponentAttack")
        await self.record(group(rounds=[{"warTags": [war_tag, "#R2"]}]), kind="cwl_group", at=at - 2 * DAY * SECOND)
        await self.record(payload, kind="cwl_war", scope=war_tag, at=at + 90 * SECOND)

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


    async def test_health_scores_join_record_regular_and_league_wars(self):
        await self.ended()
        start, end = seconds(START), seconds(END) + 4 * DAY
        await self.league_round()
        analyzer = ClanHealthAnalyzer(self.health, self.regular, self.league)
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
        broken = RegularWars(SimpleNamespace(totals=AsyncMock(side_effect=sqlite3.OperationalError("locked"))),
                             CLANS)
        rows = [{"player_tag": "#P0", "clan_code": "ONE", "war_hits_expected": 4}]
        with self.assertRaises(sqlite3.OperationalError):
            await asyncio.to_thread(ClanHealthAnalyzer(self.health, broken, self.league).apply_war_activity, rows,
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
        await self.league_round(missed=False)
        commands = PlayerCommands(self.health, self.regular, self.league)
        merged = await asyncio.to_thread(commands._player_war_attacks, "#P0", moment(start), moment(end))
        self.assertEqual([(r["war_type"], r["source"], r["end_ts"]) for r in merged],
                         [("REG", "family_record", seconds(END)), ("CWL", "family_record", start + 10)])
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


if __name__ == "__main__":
    unittest.main()
