"""Each member's capital raid weekend, saved as typed facts beside the raw raid payloads (synthetic data only)."""

from contextlib import closing
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from elbow_helper.data.reads import RaidReads
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.schema import SCHEMA_VERSION
from elbow_helper.data.store.sources import record_source
from elbow_helper.data.store.war_projection import api_time
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .test_family_store import response

BASE = 1_793_000_000_000_000
HOUR = 3600 * 1_000_000
CLAN = "#PP9"
FIRST, SECOND, THIRD = "20261009T070000.000Z", "20261016T070000.000Z", "20261023T070000.000Z"
STARTS = {text: api_time(text) for text in (FIRST, SECOND, THIRD)}
# The version before raid member lines were added.
BEFORE_LINES = 13
LINES = "SELECT player_tag,attacks,attack_limit,bonus_attack_limit,capital_loot FROM raid_member_lines ORDER BY weekend_start,player_tag"


def member(tag, attacks=6, loot=1000, **fields):
    return {"tag": tag, "name": "Synthetic name", "attacks": attacks, "attackLimit": 5, "bonusAttackLimit": 1,
            "capitalResourcesLooted": loot, **fields}


def weekend(start, members, state="ended"):
    day = {FIRST: "20261012", SECOND: "20261019", THIRD: "20261026"}[start]
    return {"state": state, "startTime": start, "endTime": day + "T070000.000Z", "capitalTotalLoot": 5, "members": members}


def body(*weekends):
    return {"items": list(weekends), "paging": {"cursors": {}}}


class RaidFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "family.sqlite3"
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        self.reads = RaidReads(self.store)
        self.at = BASE

    async def run_sql(self, sql):
        def run(connection):
            with sqlite_transaction(connection):
                connection.execute(sql)
        await self.store.call(run)

    async def rows(self, sql, args=()):
        return await self.store.call(lambda c: [dict(row) for row in c.execute(sql, args)])

    async def fetch(self, payload, *, clan=CLAN, status=200):
        self.at += HOUR
        run = await self.store.begin_run("capital_raids", "family_api", [("capital_raids", clan)], self.at)
        await self.store.requested(run, 0)
        await self.store.call(record_source, run, 0, {"source_kind": "capital_raids", "scope_id": clan, "current": False},
                              response(payload, status), self.at)
        return run, await self.store.finish(run)


class RaidLineTests(RaidFixture):
    async def test_new_weekend_saves_one_line_per_member_with_run_and_time(self):
        await self.run_sql("INSERT INTO accounts(player_tag,first_observed_at,tracking_status) VALUES ('#PP29L',1,'observed')")
        run, result = await self.fetch(body(weekend(FIRST, [member("#PP29L", 5, 700), member("#PP29Q", 6, 900)])))
        self.assertEqual(result["failed"], 0)
        self.assertEqual(await self.rows(LINES), [
            {"player_tag": "#PP29L", "attacks": 5, "attack_limit": 5, "bonus_attack_limit": 1, "capital_loot": 700},
            {"player_tag": "#PP29Q", "attacks": 6, "attack_limit": 5, "bonus_attack_limit": 1, "capital_loot": 900}])
        saved = await self.rows("SELECT l.*,a.player_tag AS owner FROM raid_member_lines l LEFT JOIN accounts a ON a.id=l.account_id "
                                "ORDER BY l.player_tag")
        self.assertEqual([(r["clan_tag"], r["weekend_start"], r["owner"], r["observed_at"], r["source_run_id"]) for r in saved],
                         [(CLAN, STARTS[FIRST], "#PP29L", self.at, run), (CLAN, STARTS[FIRST], None, self.at, run)])

    async def test_running_weekend_follows_the_payload_then_keeps_final_values(self):
        await self.fetch(body(weekend(THIRD, [member("#PP29L", 2, 100), member("#PP29Q", 0, 0)], "ongoing")))
        run, _ = await self.fetch(body(weekend(THIRD, [member("#PP29L", 4, 300), member("#PP29Q", 0, 0)], "ongoing")))
        saved = await self.rows("SELECT player_tag,attacks,capital_loot,observed_at,source_run_id FROM raid_member_lines ORDER BY player_tag")
        self.assertEqual(saved[0], {"player_tag": "#PP29L", "attacks": 4, "capital_loot": 300, "observed_at": self.at, "source_run_id": run})
        # The untouched line keeps the time and run that last changed it.
        self.assertEqual((saved[1]["attacks"], saved[1]["observed_at"]), (0, BASE + HOUR))
        final, _ = await self.fetch(body(weekend(THIRD, [member("#PP29L", 6, 500), member("#PP29Q", 3, 250)], "ended")))
        self.assertEqual([(r["attacks"], r["capital_loot"]) for r in await self.rows(LINES)], [(6, 500), (3, 250)])
        await self.fetch(body(weekend(THIRD, [member("#PP29L", 6, 500), member("#PP29Q", 3, 250)], "ended")))
        self.assertEqual([(r["attacks"], r["capital_loot"], r["source_run_id"]) for r in await self.rows(
            "SELECT * FROM raid_member_lines ORDER BY player_tag")], [(6, 500, final), (3, 250, final)])
        self.assertEqual(len(await self.rows("SELECT 1 FROM family_api_entities WHERE final_at IS NOT NULL")), 1)

    async def test_member_who_left_stays_in_the_weekend(self):
        await self.fetch(body(weekend(SECOND, [member("#PP29L", 3, 300), member("#PP29Q", 2, 200)], "ongoing")))
        await self.fetch(body(weekend(SECOND, [member("#PP29L", 5, 500)], "ended")))
        self.assertEqual([(r["player_tag"], r["attacks"], r["capital_loot"]) for r in await self.rows(LINES)],
                         [("#PP29L", 5, 500), ("#PP29Q", 2, 200)])

    async def test_repeated_fetch_changes_nothing(self):
        payload = body(weekend(FIRST, [member("#PP29L")]), weekend(SECOND, [member("#PP29L", 4, 40), member("#PP29Q")]))
        await self.fetch(payload)
        before = await self.rows("SELECT * FROM raid_member_lines ORDER BY weekend_start,player_tag")
        await self.fetch(payload)
        self.assertEqual(await self.rows("SELECT * FROM raid_member_lines ORDER BY weekend_start,player_tag"), before)
        self.assertEqual(len(before), 3)

    async def test_weekends_of_two_clans_stay_apart(self):
        await self.fetch(body(weekend(FIRST, [member("#PP29L", 1, 10)])))
        await self.fetch(body(weekend(FIRST, [member("#PP29L", 2, 20)])), clan="#PP8")
        self.assertEqual([(r["clan_tag"], r["attacks"]) for r in await self.rows(
            "SELECT * FROM raid_member_lines ORDER BY clan_tag")], [("#PP8", 2), (CLAN, 1)])

    async def test_members_without_a_safe_identity_or_count_are_left_to_the_raw_payload(self):
        members = [member("#PP29L"), {"name": "No tag"}, member("not a tag"), member("#PP29Q"), member("#PP29Q", 1),
                   member("#PP29G", "many", None, attackLimit=None), "stray", None, member(7)]
        await self.fetch(body(weekend(FIRST, members)))
        self.assertEqual(await self.rows(LINES), [
            {"player_tag": "#PP29G", "attacks": None, "attack_limit": None, "bonus_attack_limit": 1, "capital_loot": None},
            {"player_tag": "#PP29L", "attacks": 6, "attack_limit": 5, "bonus_attack_limit": 1, "capital_loot": 1000}])
        # Nothing was lost: the raw leaves still hold every member.
        self.assertTrue(await self.rows("SELECT 1 FROM api_records WHERE source_kind='capital_raids'"))

    async def test_weekend_without_a_member_list_saves_no_lines(self):
        for members in (None, "none", {}):
            payload = weekend(FIRST, [])
            payload["members"] = members
            _, result = await self.fetch(body(payload))
            self.assertEqual(result["failed"], 0)
        self.assertEqual(await self.rows(LINES), [])

    async def test_invalid_page_saves_no_lines_for_its_valid_weekends(self):
        bad = weekend(SECOND, [member("#PP29Q")])
        bad["state"] = "unknown"
        _, result = await self.fetch(body(weekend(FIRST, [member("#PP29L")]), bad))
        self.assertEqual(result["failed"], 1)
        self.assertEqual(await self.rows(LINES), [])

    async def reset_to_previous_version(self):
        for table in ("raid_member_lines", "old_clan_stays", "old_raid_weekends", "member_last_messages"):
            await self.run_sql(f"DROP TABLE {table}")
        await self.store.call(lambda c: c.execute(f"PRAGMA user_version={BEFORE_LINES}"))

    async def test_upgrade_backfills_every_saved_weekend_from_the_raw_payloads(self):
        await self.fetch(body(weekend(FIRST, [member("#PP29L", 5, 50), member("#PP29Q", 4, 40)]),
                              weekend(SECOND, [member("#PP29L", 3, 30)])))
        await self.fetch(body(weekend(THIRD, [member("#PP29G", 1, 10)], "ongoing")), clan="#PP8")
        expected = await self.rows("SELECT clan_tag,weekend_start,player_tag,attacks,attack_limit,bonus_attack_limit,capital_loot "
                                   "FROM raid_member_lines ORDER BY clan_tag,weekend_start,player_tag")
        await self.reset_to_previous_version()
        upgraded = FamilyStore(self.path)
        await upgraded.initialize()
        self.assertEqual(await upgraded.call(lambda c: c.execute("PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
        rows = await upgraded.call(lambda c: [dict(row) for row in c.execute(
            "SELECT clan_tag,weekend_start,player_tag,attacks,attack_limit,bonus_attack_limit,capital_loot "
            "FROM raid_member_lines ORDER BY clan_tag,weekend_start,player_tag")])
        self.assertEqual(rows, expected)
        self.assertEqual(len(rows), 4)
        stamped = await upgraded.call(lambda c: [dict(row) for row in c.execute(
            "SELECT l.observed_at,l.source_run_id,r.status FROM raid_member_lines l "
            "JOIN collection_runs r ON r.id=l.source_run_id ORDER BY l.clan_tag,l.weekend_start LIMIT 3")])
        self.assertTrue(all(row["observed_at"] > BASE for row in stamped))
        self.assertEqual(await upgraded.call(lambda c: c.execute("PRAGMA foreign_key_check").fetchall()), [])
        # A second open changes nothing.
        await FamilyStore(self.path).initialize()
        self.assertEqual(len(await self.rows("SELECT 1 FROM raid_member_lines")), 4)

    async def test_failed_backfill_leaves_nothing_and_the_old_version(self):
        await self.fetch(body(weekend(FIRST, [member("#PP29L")])))
        await self.fetch(body(weekend(SECOND, [member("#PP29L")])), clan="#PP8")
        await self.reset_to_previous_version()
        await self.run_sql("UPDATE api_records SET state_z=x'00' "
                           "WHERE id=(SELECT MAX(id) FROM api_records WHERE source_kind='capital_raids')")
        with self.assertRaises(SQLiteMigrationError):
            await FamilyStore(self.path).initialize()
        with closing(sqlite3.connect(self.path)) as target:
            self.assertEqual(target.execute("PRAGMA user_version").fetchone()[0], BEFORE_LINES)
            self.assertEqual(target.execute("SELECT name FROM sqlite_master WHERE name LIKE 'raid_%'").fetchall(), [])

    async def test_old_version_without_raw_weekends_upgrades_to_an_empty_table(self):
        await self.reset_to_previous_version()
        upgraded = FamilyStore(self.path)
        await upgraded.initialize()
        self.assertEqual(await upgraded.call(lambda c: c.execute("SELECT COUNT(*) FROM raid_member_lines").fetchone()[0]), 0)


class RaidReadTests(RaidFixture):
    async def window(self, clans=(CLAN,), after=None, before=None, **options):
        return await self.reads.window(clans, after=STARTS[FIRST] if after is None else after,
                                       before=STARTS[THIRD] + 1 if before is None else before, **options)

    async def test_window_lists_weekends_with_lines_and_coverage(self):
        older = weekend(FIRST, [])
        del older["members"]
        await self.fetch(body(weekend(THIRD, [member("#PP29Q", 1, 10)], "ongoing"), weekend(SECOND, []), older))
        result = await self.window()
        self.assertEqual([(w["start_at"], w["state"], w["final"], w["has_member_lines"], w["payload_listed_members"])
                          for w in result["weekends"]],
                         [(STARTS[FIRST], "ended", True, False, False), (STARTS[SECOND], "ended", True, False, True),
                          (STARTS[THIRD], "ongoing", False, True, True)])
        self.assertEqual(result["weekends"][0]["end_at"], STARTS[FIRST] + 3 * 24 * HOUR)
        self.assertEqual(result["weekends"][2]["members"][0]["capital_loot"], 10)
        self.assertEqual(result["coverage"]["with_member_lines"], 1)
        self.assertEqual(result["coverage"]["without_member_lines"], [
            {"clan_tag": CLAN, "start_at": STARTS[FIRST], "payload_listed_members": False},
            {"clan_tag": CLAN, "start_at": STARTS[SECOND], "payload_listed_members": True}])
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(result["coverage"]["sources"][CLAN]["history_complete"])

    async def test_window_bounds_clans_paging_and_unobserved_clans(self):
        await self.fetch(body(weekend(FIRST, [member("#PP29L")]), weekend(SECOND, [member("#PP29L")]),
                              weekend(THIRD, [member("#PP29L")])))
        self.assertEqual(len((await self.window())["weekends"]), 3)
        only = await self.window(after=STARTS[SECOND], before=STARTS[THIRD])
        self.assertEqual([w["start_at"] for w in only["weekends"]], [STARTS[SECOND]])
        early = await self.window(as_of=STARTS[SECOND])
        self.assertEqual([w["start_at"] for w in early["weekends"]], [STARTS[FIRST], STARTS[SECOND]])
        first = await self.window(limit=2)
        self.assertEqual((len(first["weekends"]), first["next_offset"], first["truncated"]), (2, 2, True))
        self.assertFalse(first["coverage"]["complete"])
        rest = await self.window(limit=2, offset=2)
        self.assertEqual((len(rest["weekends"]), rest["next_offset"]), (1, None))
        other = await self.window(clans=(CLAN, "#PP8"))
        self.assertEqual(other["coverage"]["unobserved_clans"], ["#PP8"])
        self.assertTrue(other["coverage"]["complete"])
        self.assertEqual(other["coverage"]["sources"]["#PP8"]["last_status"], None)
        empty = await self.window(clans=("#PP8",))
        self.assertEqual((empty["weekends"], empty["coverage"]["complete"]), ([], False))

    async def test_window_refuses_bad_arguments(self):
        for args, options in ((((), ), {}), ((("nonsense!",),), {}), (((CLAN,),), {"limit": 0}),
                              (((CLAN,),), {"offset": -1}), (((CLAN,),), {"after": 5, "before": 5}),
                              (((CLAN,),), {"after": -1, "before": 5})):
            with self.subTest(args=args, options=options):
                with self.assertRaises(ValueError):
                    await self.reads.window(*args, **{"after": 1, "before": 2, **options})


if __name__ == "__main__":
    unittest.main()
