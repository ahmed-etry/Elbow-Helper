"""Synthetic ranked reads over copied and live battles, with watch coverage and clan rules."""

import unittest

from elbow_helper.data.reads import RankedReads
from elbow_helper.data.store.clans import collect_clans

from .test_family_store import CLANS, clan_response, player
from .test_ranked_import import BASE, SECOND, RankedFixture, battle


AFTER, BEFORE, AS_OF = BASE - 3600 * SECOND, BASE + 3600 * SECOND, BASE + 300 * SECOND


class RankedReadTests(RankedFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reads = RankedReads(self.store)

    async def window(self, selector="account_tags", values=("#P0",), **kwargs):
        arguments = {"after": AFTER, "before": BEFORE, "as_of": AS_OF, "include_battles": True, **kwargs}
        return await self.reads.window(selector, values, **arguments)

    async def roster(self, offset):
        at = BASE + offset * SECOND
        specs = [(kind, clan.tag) for clan in CLANS for kind in ("clan_profile", "clan_roster")]
        run = await self.store.begin_run("clans", "family_clans", specs, at)
        for ordinal in range(4):
            await self.store.requested(run, ordinal)
        await self.store.call(collect_clans, run, CLANS, [clan_response(0, [player()]), clan_response(1, [])], at)
        await self.store.finish(run)

    async def test_copied_and_live_ranked_battles_count_once_and_other_modes_never(self):
        self.add_battle(battle(-10))
        self.add_battle(battle(-20, mode="legend", attack=False), key="legend")
        await self.copy()
        await self.live({"items": [battle(-10), battle(60), battle(70, mode="homeVillage")]}, 100)
        row = (await self.window())["accounts"][0]
        self.assertEqual((row["attacks_used"], row["defenses"], row["battle_records"], row["old_store_battles"]),
                         (2, 1, 3, 2))
        self.assertEqual(row["member_id"], 100)
        self.assertEqual(row["daily"], [{"date": "2026-10-15", "attacks_used": 1, "defenses": 1},
                                        {"date": "2026-10-16", "attacks_used": 1, "defenses": 0}])
        self.assertEqual({b["payload"]["battleType"] for b in row["battles"]}, {"ranked", "legend"})
        self.assertEqual([b["old_store"] for b in row["battles"]], [True, True, False])
        self.assertFalse(row["counts_complete"])
        later = (await self.window(after=BASE))["accounts"][0]
        self.assertEqual(later["attacks_used"], 1)

    async def test_coverage_names_the_tail_before_the_first_read_and_the_unread_end(self):
        self.add_battle(battle(-10))
        await self.copy()
        coverage = (await self.window())["accounts"][0]["log_coverage"]
        self.assertEqual(coverage["watched_from"], BASE)
        self.assertEqual([gap["cause"] for gap in coverage["gaps"]], ["before_first_read", "not_yet_read"])
        self.assertEqual(coverage["gaps"][0], {"after": AFTER, "before": BASE, "cause": "before_first_read"})
        self.assertEqual(coverage["gaps"][1]["before"], AS_OF)
        self.assertEqual(coverage["status"], "partial")
        overdue = (await self.window(as_of=BASE + 3 * 3600 * SECOND, before=BASE + 4 * 3600 * SECOND))
        self.assertEqual(overdue["accounts"][0]["log_coverage"]["gaps"][-1]["cause"], "overdue")

    async def test_failed_reads_and_a_gap_between_watchers_are_gaps_not_zeroes(self):
        await self.copy()
        await self.live({"items": []}, 100)
        await self.live(None, 120, status=0)
        await self.live({"items": []}, 140)
        coverage = (await self.window())["accounts"][0]["log_coverage"]
        causes = {gap["cause"]: gap for gap in coverage["gaps"]}
        self.assertEqual((causes["log_not_read"]["after"], causes["log_not_read"]["before"]),
                         (BASE + 100 * SECOND, BASE + 140 * SECOND))
        self.assertEqual((causes["between_watchers"]["after"], causes["between_watchers"]["before"]),
                         (BASE, BASE + 100 * SECOND))
        self.assertEqual(coverage["failed_reads_in_window"], 1)
        self.assertEqual(coverage["watchers"], ["legacy_ranked", "record"])

    async def test_an_account_never_read_has_unknown_counts_and_unmatched_members_are_listed(self):
        result = await self.window(values=("#P9",))
        row = result["accounts"][0]
        self.assertIsNone(row["attacks_used"])
        self.assertEqual(row["log_coverage"]["status"], "never_read")
        self.assertEqual(row["log_coverage"]["gaps"], [{"after": AFTER, "before": AS_OF, "cause": "never_read"}])
        members = await self.window("member_ids", (100, 999))
        self.assertEqual([row["player_tag"] for row in members["accounts"]], ["#P0"])
        self.assertEqual(members["unobserved_selections"], [999])

    async def test_clans_use_record_membership_once_watched_and_the_old_first_seen_clan_before(self):
        self.add_battle(battle(-10), clan="#LQ")
        await self.copy()
        await self.roster(50)
        await self.live({"items": [battle(60)]}, 100)
        second = (await self.window("clan_tags", ("#LQ",)))["accounts"][0]
        first = (await self.window("clan_tags", ("#PY",)))["accounts"][0]
        self.assertEqual((second["attacks_used"], first["attacks_used"]), (1, 1))
        self.assertEqual([(b["clan_tag"], b["clan_basis"]) for b in second["battles"]], [("#LQ", "clan_when_first_seen")])
        self.assertEqual([(b["clan_tag"], b["clan_basis"]) for b in first["battles"]], [("#PY", "record_membership")])
        self.assertEqual(first["clan_tag"], "#PY")
        empty = await self.window("clan_tags", ("#GR",))
        self.assertEqual((empty["accounts"], empty["unobserved_selections"]), ([], ["#GR"]))

    async def test_invalid_selections_windows_and_pages_are_refused(self):
        for selector, values, kwargs in (("names", ("#P0",), {}), ("member_ids", (0,), {}), ("account_tags", ("bad tag!",), {}),
                                         ("account_tags", (), {}), ("account_tags", ("#P0",), {"before": AFTER}),
                                         ("account_tags", ("#P0",), {"limit": 0})):
            with self.subTest(selector=selector, values=values, kwargs=kwargs), self.assertRaises(ValueError):
                await self.window(selector, values, **kwargs)
