"""Health window policy is exercised through the feature adapter, on synthetic wars."""

import unittest
from elbow_helper.data.reads import WarReads
from elbow_helper.features.clan_health.record_wars import capture_page
from tests.data.test_wars import WarFixture, war, START, END, SECOND
from tests.data.test_war_reads import friendly, DAY, HOUR


class CaptureReadTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reads = WarReads(self.store)

    async def test_health_renumbers_native_league_positions_only_in_the_adapter(self):
        payload = war("warEnded")
        for index, member in enumerate(payload["opponent"]["members"], 1):
            member["mapPosition"] = index * 10
        await self.record(payload, kind="cwl_war", scope="#R0", at=END + SECOND)
        result = await capture_page(self.reads, "attacks", ["#PY"], END, END + 2 * SECOND,
                                    types=("CWL",), as_of=END + SECOND)
        self.assertEqual([row["defender_position"] for row in result["rows"]], [1, 2])
        self.assertEqual([row["defender_map_position"] for row in result["rows"]], [10, 20])

    async def capture(self, method, clans, after, before, *, view="capture", **kwargs):
        if view == "capture":
            result = await capture_page(self.reads, method, clans, after, before + 1, **kwargs)
        elif view == "completed":
            result = await getattr(self.reads, method)(clans, after, before, **kwargs)
        else:
            raise ValueError("Unknown test view")
        for row in result.get("rows", []):
            if "time_basis" in row:
                row["time_basis"] = {"end_at": "war_end", "observed_at": "last_check"}[row["time_basis"]]
        result.update(view=view, bounds="inclusive" if view == "capture" else "exclusive_end")
        return result

    async def test_health_views_keep_capture_time_and_completed_meanings(self):
        await self.record(war("preparation"), at=START - HOUR)
        await self.record(war(), at=self.at)
        capture = await self.capture("participation", ["#PY"], START, self.at, as_of=self.at)
        self.assertEqual([(r["player_tag"], r["state"], r["time"], r["time_basis"], r["attacks_used"],
                           r["attacks_expected"], r["stars"]) for r in capture["rows"]],
                         [("#P0", "inWar", self.at, "last_check", 1, 2, 3), ("#P2", "inWar", self.at, "last_check", 0, 2, 0)])
        self.assertEqual(capture["bounds"], "inclusive")
        self.assertEqual((await self.capture("participation", ["#PY"], START, self.at - 1, as_of=self.at))["rows"], [])
        self.assertEqual((await self.capture("participation", ["#PY"], START, END + DAY, view="completed",
                                                           as_of=self.at))["rows"], [])
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.record(friendly(), at=END + 3 * DAY)
        at = END + 4 * DAY
        ended = await self.capture("participation", ["#PY"], END, END + 3 * DAY, view="completed", as_of=at)
        self.assertEqual({(r["time"], r["time_basis"], r["type"]) for r in ended["rows"]}, {(END, "war_end", "REG")})
        self.assertEqual((await self.capture("participation", ["#PY"], START, END, view="completed", as_of=at))["rows"], [])
        self.assertEqual(len((await self.capture("participation", ["#PY"], START, END, as_of=at))["rows"]), 2)
        with_friendly = await self.capture("participation", ["#PY"], END, END + 3 * DAY, types=["REG", "FRIENDLY"], as_of=at)
        self.assertEqual(sorted({r["type"] for r in with_friendly["rows"]}), ["FRIENDLY", "REG"])
        # League wars are read only when asked for; there are none here.
        self.assertEqual((await self.capture("participation", ["#PY"], END, END + DAY, types=["CWL"], as_of=at))["rows"], [])
        with self.assertRaises(ValueError):
            await self.capture("participation", ["#PY"], END, END + DAY, types=["ARRANGED"])

    async def test_health_totals_sum_each_account_over_one_snapshot_and_keep_unknown_counts_apart(self):
        await self.record(war(), at=self.at)
        # The ongoing war counts as last captured, its attacks so far included.
        capture = await self.capture("totals", ["#PY"], START, self.at, player_tags=["#P0", "#P2", "#Q0"], as_of=self.at)
        self.assertEqual([(r["player_tag"], r["wars"], r["attacks_expected"], r["attacks_used"], r["stars"],
                           r["attacks_measured"], r["wars_without_counts"]) for r in capture["rows"]],
                         [("#P0", 1, 2, 1, 3, 1, 0), ("#P2", 1, 2, 0, 0, 0, 0)])
        self.assertEqual((capture["view"], capture["bounds"], capture["coverage"]["wars_selected"]), ("capture", "inclusive", 1))
        self.assertEqual((await self.capture("totals", ["#PY"], START, self.at, view="completed",
                                                         player_tags=["#P0"], as_of=self.at))["rows"], [])
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.record(friendly(), at=END + 3 * DAY)
        at = END + 4 * DAY
        ended = await self.capture("totals", ["#PY"], START, at, player_tags=["#P0"], as_of=at)
        self.assertEqual([(r["wars"], r["attacks_expected"], r["attacks_used"], r["triples"], r["destruction"])
                          for r in ended["rows"]], [(1, 2, 1, 1, 100.0)])
        # A war that does not know a count adds the war, never a used or missed attack.
        await self.execute("UPDATE war_members SET attacks_expected=NULL WHERE player_tag='#P0'")
        (unknown,) = (await self.capture("totals", ["#PY"], START, at, player_tags=["#P0"], as_of=at))["rows"]
        self.assertEqual((unknown["wars"], unknown["wars_without_counts"], unknown["attacks_expected"],
                          unknown["attacks_used"]), (1, 1, 0, 0))
        # Friendly wars are counted only when asked for.
        both = await self.capture("totals", ["#PY"], START, at, player_tags=["#P2"], types=["REG", "FRIENDLY"], as_of=at)
        self.assertEqual(both["rows"][0]["wars"], 2)
        counts = await self.capture("counts", ["#PY", "#LQ"], START, at, as_of=at)
        self.assertEqual(counts["clans"], {"#PY": {"wars": 1, "wars_with_lineup": 1},
                                           "#LQ": {"wars": 0, "wars_with_lineup": 0}})
        self.assertEqual((await self.capture("counts", ["#PY"], START, at, types=["REG", "FRIENDLY"],
                                                             as_of=at))["clans"]["#PY"]["wars"], 2)
        self.assertEqual((await self.capture("counts", ["#PY"], START, at, types=["CWL"],
                                                             as_of=at))["clans"]["#PY"]["wars"], 0)
        # A war known only from the log has no lineup to count.
        await self.execute("DELETE FROM war_members WHERE war_id=(SELECT war_id FROM war_sides WHERE clan_tag='#PY' "
                           "AND war_id IN (SELECT id FROM wars WHERE type='REG'))")
        self.assertEqual((await self.capture("counts", ["#PY"], START, at, as_of=at))["clans"]["#PY"],
                         {"wars": 1, "wars_with_lineup": 0})
        for call in (lambda: self.capture("totals", ["#PY"], START, END, player_tags=[]),
                     lambda: self.capture("totals", ["#PY"], START, END, player_tags=["#P0"], view="live"),
                     lambda: self.capture("counts", ["#PY"], START, END, types=["ARRANGED"])):
            with self.assertRaises(ValueError):
                await call()

