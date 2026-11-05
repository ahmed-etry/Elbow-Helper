"""Neutral facts, symmetric war completeness and evidence on empty or failed reads."""

import sqlite3
import unittest
from unittest.mock import patch

from elbow_helper.data.reads import CwlReads, MembershipReads, PeopleReads, WarReads
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.schema import SCHEMA_VERSION
from elbow_helper.data.store.war_completeness import upgrade_cwl_completeness, war_side_coverage
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError
from tests.data import test_family_store as family
from tests.data import test_people_reads as people_fixtures
from tests.data.test_wars import END, SECOND, START, WarFixture, group, war
from tests.data.test_wars_import_cwl import CwlFixture, FROZEN


class NeutralWarTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def test_observation_bounds_are_half_open_and_counts_remain_unknown(self):
        await self.record(war(), at=self.at)
        reads = WarReads(self.store)
        empty = await reads.totals(["#PY"], START, self.at, time_field="observed_at",
                                   player_tags=["#P0"], as_of=self.at)
        self.assertEqual(empty["rows"], [])
        found = await reads.participation(["#PY"], START, self.at + 1, time_field="observed_at", as_of=self.at)
        self.assertEqual({row["time"] for row in found["rows"]}, {self.at})
        self.assertTrue(all(row["missed"] is None for row in found["rows"]))
        await self.execute("UPDATE war_members SET attacks_expected=NULL WHERE player_tag='#P0'")
        (unknown,) = (await reads.totals(["#PY"], START, self.at + 1, time_field="observed_at",
                                        player_tags=["#P0"], as_of=self.at))["rows"]
        self.assertEqual((unknown["wars"], unknown["wars_without_counts"], unknown["attacks_used"]), (1, 1, 0))
        self.assertFalse(found["coverage"]["complete"])

    async def test_time_state_unions_are_one_snapshot_and_deduplicate(self):
        await self.record(war("warEnded"), at=END + SECOND)
        reads = WarReads(self.store)
        choices = (("end_at", ("warEnded",)), ("observed_at", ("warEnded",)))
        result = await reads.counts(["#PY"], START, END + 2 * SECOND, time_field=choices, as_of=END + SECOND)
        self.assertEqual(result["clans"]["#PY"]["wars"], 1)
        for field, states in (("capture", None), ("end_at", ()), ("end_at", ("unknown",)),
                              ((), None), ((("capture", None),), None), ((("end_at", None),), ("inWar",))):
            with self.subTest(field=field, states=states), self.assertRaises(ValueError):
                await reads.counts(["#PY"], START, END, time_field=field, states=states)

    async def test_native_attack_positions_survive_and_failed_sources_are_explicit(self):
        payload = war("warEnded")
        for side in ("clan", "opponent"):
            for index, member in enumerate(payload[side]["members"], 1):
                member["mapPosition"] = index * 10
        await self.record(payload, at=END + SECOND)
        result = await WarReads(self.store).attacks(["#PY"], END, END + 2 * SECOND, as_of=END + SECOND)
        self.assertEqual([row["defender_position"] for row in result["rows"]], [10, 20])
        self.assertEqual([row["map_position"] for row in result["lineups"][0]["members"]], [10, 20])
        with patch.object(self.store, "call", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                await WarReads(self.store).log_entries("#PY")


class LeagueEvidenceTests(CwlFixture, unittest.IsolatedAsyncioTestCase):
    async def test_coverage_tracks_missing_rounds_and_failures_only_for_relevant_wars(self):
        await self.record(group(rounds=[{"warTags": ["#R0", "#R2", "#R8"]}]), kind="cwl_group", at=START - SECOND)
        await self.record(war("warEnded"), kind="cwl_war", scope="#R0", at=END + SECOND)
        await self.record(war("warEnded", clans=("#LQ", "#CU")), kind="cwl_war", scope="#R2", at=END + SECOND)
        await self.record(None, kind="cwl_war", scope="#R0", status=503, at=END + 2 * SECOND)
        reads = CwlReads(self.store)
        snapshot = await reads.group("#PY", END + 2 * SECOND)
        self.assertEqual(snapshot["coverage"]["missing_wars"], ["#R8"])
        self.assertNotIn("#R2", [source["scope"] for source in snapshot["coverage"]["signals"]])
        weeks = await reads.weeks(["#PY"], as_of=END + 2 * SECOND)
        self.assertEqual([row["war_tag"] for row in weeks["coverage"]["missing_wars"]], ["#R8"])
        self.assertEqual(weeks["weeks"][0]["wars"][0]["source_coverage"]["signals"][0]["last_outcome"], "failed")

    async def test_failed_completeness_upgrade_rolls_back_and_retries(self):
        await self.week()
        await self.run_cwl()
        first = (await self.rows("SELECT id FROM wars ORDER BY id"))[0]["id"]
        evidence = await self.store.call(war_side_coverage, first, "#PY")
        await self.execute("UPDATE wars SET roster_complete=1,attacks_complete=1")
        await self.execute("DROP TABLE member_last_messages")
        await self.execute("PRAGMA user_version=15")
        fresh = FamilyStore(self.path)
        with patch("elbow_helper.data.store.war_completeness.war_side_coverage",
                   side_effect=[evidence, sqlite3.OperationalError("interrupted")]):
            with self.assertRaises(SQLiteMigrationError):
                await fresh.initialize()
        self.assertEqual((await self.rows("PRAGMA user_version"))[0]["user_version"], 15)
        self.assertTrue(all(tuple(row.values()) == (1, 1) for row in
                            await self.rows("SELECT roster_complete,attacks_complete FROM wars")))
        await fresh.initialize()
        self.assertEqual((await self.rows("PRAGMA user_version"))[0]["user_version"], SCHEMA_VERSION)

    async def test_import_flags_and_upgrade_describe_both_sides_without_inventing_counts(self):
        await self.week(positions=(30, 40), defender_positions=(31, 41))
        await self.run_cwl()
        wars = await self.rows("SELECT * FROM wars WHERE type='CWL'")
        self.assertTrue(all((row["roster_complete"], row["attacks_complete"]) == (0, 0) for row in wars))
        sides = await self.store.call(war_side_coverage, wars[0]["id"], "#PY")
        reverse = await self.store.call(war_side_coverage, wars[0]["id"], sides["opponent"]["clan_tag"])
        self.assertEqual(sides["own"], reverse["opponent"])
        self.assertTrue(sides["own"]["attacks_complete"])
        self.assertFalse(sides["opponent"]["attacks_complete"])
        self.assertIsNone(sides["opponent"]["attacks_reported"])
        await self.execute("UPDATE wars SET roster_complete=1,attacks_complete=1")
        await self.execute("DROP TABLE member_last_messages")
        await self.execute("PRAGMA user_version=15")
        fresh = FamilyStore(self.path)
        await fresh.initialize()
        await fresh.call(upgrade_cwl_completeness)
        repaired = await self.rows("SELECT roster_complete,attacks_complete FROM wars")
        self.assertTrue(all(tuple(row.values()) == (0, 0) for row in repaired))
        self.assertEqual((await self.rows("PRAGMA user_version"))[0]["user_version"], SCHEMA_VERSION)
        self.assertIsNone((await fresh.call(war_side_coverage, wars[0]["id"], "#PY"))["opponent"]["attacks_reported"])

    async def test_native_league_rows_keep_positions_times_and_per_war_evidence(self):
        await self.week(positions=(30, 40), defender_positions=(31, 41))
        await self.run_cwl()
        reads = CwlReads(self.store)
        result = await reads.weeks(["#PY"], as_of=FROZEN)
        row = result["weeks"][0]["wars"][0]
        self.assertGreater(row["end_at"], 1_000_000_000_000)
        self.assertNotIn("end_ts", row)
        self.assertNotIn("roster", row)
        self.assertEqual(sorted(member["source_map_position"] for member in row["members"]
                                if member["side"] == row["own_side"]), [30, 40])
        self.assertFalse(row["coverage"]["opponent"]["lineup_complete"])
        self.assertIn("signals", row["source_coverage"])
        self.assertTrue(result["coverage"]["incomplete_wars"])
        names = await reads.account_names(["#PY"], FROZEN)
        self.assertTrue(names["rows"])
        self.assertFalse(names["coverage"]["name_history_complete"])
        resolved = await reads.resolve([row["war_tag"], "missing"], FROZEN)
        self.assertEqual(resolved["matches"]["missing"], [])
        self.assertEqual(resolved["coverage"]["unresolved_keys"], ["missing"])
        self.assertFalse(resolved["coverage"]["complete"])

    async def test_empty_and_failed_league_reads_never_claim_no_history(self):
        reads = CwlReads(self.store)
        for result in (await reads.weeks(["#PY"], as_of=FROZEN),
                       await reads.account_names(["#PY"], FROZEN), await reads.group("#PY", FROZEN),
                       await reads.resolve(["missing"], FROZEN)):
            self.assertFalse(result["coverage"]["complete"])
            self.assertTrue(result["coverage"]["signals"] or result["coverage"].get("unresolved_keys"))
        with patch.object(self.store, "call", side_effect=sqlite3.OperationalError("locked")):
            for coroutine in (reads.weeks(["#PY"]), reads.account_names(["#PY"]),
                              reads.resolve(["missing"]), reads.group("#PY")):
                with self.assertRaises(sqlite3.OperationalError):
                    await coroutine


class PeopleEvidenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = people_fixtures.PeopleReadTests.asyncSetUp
    clans = family.FamilyStoreTests.clans
    links = family.FamilyStoreTests.links

    async def test_history_and_generations_describe_missing_and_failed_observations(self):
        people = PeopleReads(self.store, clans=family.CLANS)
        history = await people.account_history(["#P0"], 100)
        self.assertEqual(history["coverage"]["unobserved_accounts"], ["#P0"])
        self.assertFalse(history["coverage"]["complete"])
        await self.clans(100)
        await self.clans(200, [family.response({}, 503), family.clan_response(1, [])])
        result = await self.membership.generations(0, 201)
        self.assertEqual([row["at"] for row in result["rows"]], [100])
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(any(source["failures"] for source in result["coverage"]["signals"]))
        rosters = await self.membership.rosters(as_of=200)
        self.assertFalse(rosters["coverage"]["complete"])
        with self.assertRaises(ValueError):
            await self.membership.generations(2, 1)
        empty = await MembershipReads(self.store, clans=()).generations(0, 1)
        self.assertFalse(empty["coverage"]["complete"])

    async def test_current_links_without_import_return_unavailable_coverage(self):
        result = await self.people.current_links(100)
        self.assertEqual(result["rows"], [])
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["unavailable"], "people_import_not_started")
