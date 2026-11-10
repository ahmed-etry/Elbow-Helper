"""A war-log entry whose end drifts minutes from the live capture's is the same war.

Synthetic wars go through the collectors' own storage path. No Clash call is made.
"""

import unittest
from unittest.mock import patch

from elbow_helper.agent.reads.record_wars import combine
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store import wars as war_store
from elbow_helper.data.store.schema import MIGRATIONS, SCHEMA_VERSION
from elbow_helper.data.store.war_schema import merge_drifted_log_copies
from elbow_helper.domain.war_views import war_entry

from .test_wars import END, SECOND, WarFixture, war, war_log

# Clash gave the live war and its log entry ends 138 seconds apart.
DRIFTED_END = "20261028T180218.000Z"
TABLES = ("wars", "war_sides", "war_members", "war_attacks", "war_source_keys", "war_collection_state")


def drifted(payload=None, end=DRIFTED_END):
    page = war_log(war("warEnded") if payload is None else payload)
    page["items"][0]["endTime"] = end
    return page


def later_war(state="warEnded"):
    """The same two clans' next war, prepared the moment the first one ended."""
    payload = war(state, end="20261030T180000.000Z")
    payload.update(preparationStartTime="20261028T180000.000Z", startTime="20261029T180000.000Z")
    return payload


def rival_war():
    """Another war of the same two clans ending five minutes after the first, as an ambiguous case."""
    payload = war("warEnded", end="20261028T180500.000Z")
    payload["preparationStartTime"] = "20261026T170000.000Z"
    return payload


class LogMatchTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def log(self, page, at=END + 86400 * SECOND):
        await self.execute("UPDATE war_collection_state SET checkpoint_json='{}' WHERE source_kind='war_log'")
        await self.record(page, kind="war_log", at=at)

    async def dump(self):
        return {table: await self.rows(f"SELECT * FROM {table} ORDER BY 1,2") for table in TABLES}

    async def test_a_log_entry_minutes_after_the_live_end_joins_the_live_war(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        attacks = await self.rows("SELECT * FROM war_attacks ORDER BY attack_order")
        await self.log(drifted())
        (row,) = await self.rows("SELECT * FROM wars")
        self.assertEqual((row["end_at"], row["type"], row["type_basis"]), (END, "REG", "war_log"))
        self.assertEqual({r["source_kind"] for r in await self.rows("SELECT source_kind FROM war_source_keys")},
                         {"preparation", "end", "war_log"})
        # The log fills in the result; the lineup and attacks stay as captured.
        self.assertEqual([r["result"] for r in await self.rows("SELECT result FROM war_sides ORDER BY side")],
                         ["tie", "tie"])
        self.assertEqual(len(await self.rows("SELECT * FROM war_members")), 4)
        self.assertEqual(attacks, await self.rows("SELECT * FROM war_attacks ORDER BY attack_order"))

    async def test_a_live_capture_after_a_drifted_log_copy_joins_it(self):
        await self.log(drifted(), at=END + 300 * SECOND)
        await self.record(war("warEnded"), at=END + 600 * SECOND)
        (row,) = await self.rows("SELECT * FROM wars")
        self.assertIsNotNone(row["preparation_at"])
        self.assertEqual(len(await self.rows("SELECT * FROM war_members")), 4)

    async def test_the_same_clans_next_war_stays_separate(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.record(later_war("inWar"), at=END + 86400 * SECOND)
        await self.log(drifted())
        rows = await self.rows("SELECT id,preparation_at,state FROM wars ORDER BY id")
        self.assertEqual([r["state"] for r in rows], ["warEnded", "inWar"])
        logged = await self.rows("SELECT war_id FROM war_source_keys WHERE source_kind='war_log'")
        self.assertEqual(logged, [{"war_id": rows[0]["id"]}])
        # The next war's own log entry, two days on, never joins the first war.
        await self.log(drifted(later_war(), "20261030T180218.000Z"), at=END + 3 * 86400 * SECOND)
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 2)
        self.assertEqual(len(await self.rows("SELECT * FROM war_source_keys WHERE source_kind='war_log'")), 2)
        # A log entry days from any stored war of the pair is a war of its own.
        await self.log(drifted(end="20261024T180000.000Z"), at=END + 4 * 86400 * SECOND)
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 3)

    async def test_a_log_entry_two_wars_could_be_stays_separate(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.record(rival_war(), at=END + 300 * SECOND)
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 2)
        before = await self.rows("SELECT * FROM wars ORDER BY id")
        await self.log(drifted(end="20261028T181000.000Z"))
        await self.log(drifted(end="20261028T181000.000Z"), at=END + 2 * 86400 * SECOND)
        rows = await self.rows("SELECT * FROM wars ORDER BY id")
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows[-1]["preparation_at"])
        self.assertEqual([r["type_basis"] for r in rows[:2]], [r["type_basis"] for r in before])

    async def old_duplicate(self, *others):
        """A live war and its drifted log entry saved apart, as matching within a minute did."""
        for payload in (war("warEnded"), *others):
            await self.record(payload, at=END + 600 * SECOND)
        with patch.object(war_store, "LOG_MATCH_WINDOW", war_store.END_DRIFT):
            await self.log(drifted())
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 2 + len(others))

    async def test_the_upgrade_merges_a_drifted_log_copy_into_its_war(self):
        await self.old_duplicate()
        # A war of other clans is left exactly as it was.
        await self.record(war("warEnded", clans=("#PY", "#CU")), at=END + 90 * SECOND)
        live, copy, other = await self.rows("SELECT * FROM wars ORDER BY id")
        untouched = await self.rows("SELECT * FROM war_sides WHERE war_id=? ORDER BY side", (other["id"],))
        members = await self.rows("SELECT * FROM war_members WHERE war_id=? ORDER BY id", (live["id"],))
        attacks = await self.rows("SELECT * FROM war_attacks WHERE war_id=? ORDER BY attack_order", (live["id"],))
        await self.execute("UPDATE war_collection_state SET war_id=? WHERE source_kind='current_war'", (copy["id"],))
        await self.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        self.assertEqual(await self.store.call(lambda c: c.execute("PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
        self.assertEqual([r["id"] for r in await self.rows("SELECT id FROM wars ORDER BY id")], [live["id"], other["id"]])
        merged = (await self.rows("SELECT * FROM wars WHERE id=?", (live["id"],)))[0]
        self.assertEqual((merged["end_at"], merged["preparation_at"], merged["type"], merged["type_basis"]),
                         (END, live["preparation_at"], "REG", "war_log"))
        self.assertEqual(await self.rows("SELECT DISTINCT war_id FROM war_source_keys WHERE war_id IN (?,?)",
                                         (live["id"], copy["id"])), [{"war_id": live["id"]}])
        self.assertEqual(len(await self.rows("SELECT * FROM war_source_keys WHERE source_kind='war_log' AND war_id=?",
                                             (live["id"],))), 1)
        self.assertEqual([r["result"] for r in await self.rows("SELECT result FROM war_sides WHERE war_id=? ORDER BY side",
                                                              (live["id"],))], ["tie", "tie"])
        self.assertEqual(await self.rows("SELECT DISTINCT war_id FROM war_collection_state WHERE source_kind='current_war' "
                                         "AND war_id IS NOT NULL"), [{"war_id": live["id"]}])
        self.assertEqual(await self.rows("SELECT * FROM war_sides WHERE war_id=?", (copy["id"],)), [])
        self.assertEqual(members, await self.rows("SELECT * FROM war_members WHERE war_id=? ORDER BY id", (live["id"],)))
        self.assertEqual(attacks, await self.rows("SELECT * FROM war_attacks WHERE war_id=? ORDER BY attack_order",
                                                  (live["id"],)))
        self.assertEqual(untouched, await self.rows("SELECT * FROM war_sides WHERE war_id=? ORDER BY side", (other["id"],)))
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])
        self.assertEqual((MIGRATIONS[-1].version, MIGRATIONS[-1].apply), (SCHEMA_VERSION, merge_drifted_log_copies))

    async def test_the_upgrade_leaves_a_copy_two_wars_could_own(self):
        await self.old_duplicate(rival_war())
        before = await self.dump()
        self.assertEqual(await self.store.call(merge_drifted_log_copies), 0)
        self.assertEqual(before, await self.dump())

    async def test_the_upgrade_changes_nothing_without_duplicates(self):
        self.assertEqual(await self.store.call(merge_drifted_log_copies), 0)
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.log(drifted())
        await self.record(later_war("inWar"), at=END + 86400 * SECOND)
        await self.log(drifted(end="20261020T180000.000Z"), at=END + 2 * 86400 * SECOND)
        before = await self.dump()
        self.assertEqual(await self.store.call(merge_drifted_log_copies), 0)
        self.assertEqual(before, await self.dump())


class WarListTests(unittest.TestCase):
    def test_league_and_regular_lists_keep_names_through_selection(self):
        first = war_entry("REG:#PY:1:#GR", "Hellbow", "persian gulf", 1793448000)
        second = war_entry("CWL:#R0", "Hellbow", "Rival", 1793534400)
        self.assertEqual(first, {"war_id": "REG:#PY:1:#GR", "clan_name": "Hellbow",
                                 "opponent_name": "persian gulf", "end_date": "2026-10-31"})
        self.assertEqual(war_entry("CWL:#R2", "", None, None),
                         {"war_id": "CWL:#R2", "clan_name": None, "opponent_name": None, "end_date": None})
        coverage = {"exact_ranking": False, "regular_wars_not_checked_for_friendly": 0, "friendly_wars_not_counted": 0}
        regular = {"rows": [], "war_ids": [first["war_id"]], "war_aliases": [], "wars_without_rosters": [first],
                   "identity_conflicts": [], "duplicate_identities_removed": 0, "coverage": coverage}
        league = {"rows": [], "war_ids": [second["war_id"], "CWL:#R2"], "war_aliases": [],
                  "wars_without_rosters": [second], "unproven_war_ids": [second], "coverage": {}}
        both = combine(regular, league)
        self.assertEqual(both["wars_without_rosters"], [second, first])
        self.assertEqual(both["unproven_war_ids"], [second])
        # A war left out of the selection leaves its entry out too.
        only = combine(regular, {**league, "war_ids": ["CWL:#R2"]})
        self.assertEqual(only["wars_without_rosters"], [first])
        self.assertEqual(only["unproven_war_ids"], [])


if __name__ == "__main__":
    unittest.main()
