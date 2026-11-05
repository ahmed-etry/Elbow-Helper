"""Synthetic copy of old raid lines, clan stays and former-member names into the family record."""

import asyncio
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import patch

from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.store import FamilyStore, health_import, profile_import, ranked_import, wars_import
from elbow_helper.data.store.health_import import snapshot_for_record, weekend_id, write_health
from elbow_helper.data.store.legacy_links import ImportMismatch
from elbow_helper.data.store.link_state import receipt
from elbow_helper.data.store.people import account, account_name
from elbow_helper.data.store.schema import MIGRATIONS
from elbow_helper.data.store.war_projection import api_time
from tests.data.legacy_health_schema import create_schema as health_schema
from elbow_helper.infrastructure.persistence.migrations import run_sqlite_migrations
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .legacy_ranked_schema import create_schema as ranked_schema
from .test_raid_lines import CLAN, FIRST, STARTS, RaidFixture, body, member, weekend

US = 1_000_000
DAY = 24 * 3600
HOUR = 3600
OTHER = "#PP8"
CLANS = {"ONE": CLAN, "TWO": OTHER}
# Captures run in late October 2026, in whole seconds as the old file keeps them.
T = 1_793_000_000
# The record's first weekend ends three days after it starts; an old-only weekend lies a week earlier.
FIRST_END = (STARTS[FIRST] + 3 * DAY * US) // US
OLD_END = FIRST_END - 7 * DAY


def stamp(seconds):
    """A time as Clash writes it."""
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y%m%dT%H%M%S.000Z")


STAYS = "SELECT a.player_tag,s.clan_tag,s.from_at,s.to_at,s.last_observed_at,s.captures,s.end_basis FROM old_clan_stays s " \
        "JOIN accounts a ON a.id=s.account_id ORDER BY a.player_tag,s.from_at"


class HealthFixture(RaidFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.health = self.path.parent / "health.db"
        with closing(sqlite3.connect(self.health)) as c, c:
            health_schema(c)
            c.execute("PRAGMA user_version=2")

    def old(self, sql, args=()):
        with closing(sqlite3.connect(self.health)) as c, c:
            c.execute(sql, args)

    def snap(self, seconds, tag, code="ONE", name="Old name"):
        self.old("INSERT INTO player_snapshots(captured_ts,clan_code,player_tag,player_name) VALUES (?,?,?,?)",
                 (seconds, code, tag, name))

    def directory(self, tag, name="Old name"):
        self.old("INSERT INTO player_directory VALUES (?,?,'ONE',0,?,?,'one',?,?)", (tag, name, name.lower(), tag, T, T))

    def raid(self, end, tag, code="ONE", clan=CLAN, attacks=6, limit=5, bonus=1, loot=1000, ident=None):
        self.old("INSERT INTO raid_member_activity(weekend_id,clan_code,clan_tag,end_ts,player_tag,player_name,attacks,"
                 "attack_limit,bonus_attack_limit,attacks_expected,loot,source) VALUES (?,?,?,?,?,'Raider',?,?,?,?,?,"
                 "'capitalraidseasons_api')",
                 (ident or weekend_id(clan, end), code, clan, end, tag, attacks, limit, bonus, limit + bonus, loot))

    async def accounts(self, *tags, named=None):
        def write(c):
            with sqlite_transaction(c, immediate=True):
                item = receipt(c, "players", "synthetic", 1, 1)
                for tag in tags:
                    identifier = account(c, tag, 1)
                    if named and tag in named:
                        account_name(c, identifier, named[tag], 1, item)
        await self.store.call(write)

    async def membership(self, tag, from_at):
        def write(c):
            with sqlite_transaction(c, immediate=True):
                item = receipt(c, "clans", "synthetic", from_at, 1)
                c.execute("INSERT OR IGNORE INTO clans(tag,family_code,name,is_family,wiring_revision) "
                          "VALUES (?,'ONE','Synthetic clan',1,1)", (CLAN,))
                clan = c.execute("SELECT id FROM clans WHERE tag=?", (CLAN,)).fetchone()[0]
                c.execute("INSERT INTO clan_membership(account_id,clan_id,clan_role,from_at,last_observed_at,source_item_id) "
                          "VALUES (?,?,'member',?,?,?)", (account(c, tag, from_at), clan, from_at, from_at, item))
        await self.store.call(write)

    async def copy(self):
        def operation(c):
            frozen = snapshot_for_record(c, self.health, CLANS)
            with sqlite_transaction(c, immediate=True):
                return write_health(c, frozen)
        return (await self.store.call(operation))["checks"]

    async def dump(self):
        return await self.store.call(lambda c: tuple(c.iterdump()))

    async def refused(self, reason):
        before = await self.dump()
        source = sha256(self.health.read_bytes()).digest()
        with self.assertRaisesRegex(ImportMismatch, reason):
            await self.copy()
        self.assertEqual(await self.dump(), before)
        self.assertEqual(sha256(self.health.read_bytes()).digest(), source)


class StayTests(HealthFixture):
    async def test_consecutive_captures_make_stays_with_bounces_and_two_clan_captures_left_out(self):
        for offset, code in enumerate(("ONE", "ONE", "TWO", "ONE", "ONE", "ONE")):
            self.snap(T + offset * HOUR, "#PP29L", code)
        # One capture in both clans at once says nothing about which; the stay around it goes on.
        self.snap(T + 4 * HOUR, "#PP29L", "TWO")
        await self.accounts("#PP29L")
        checks = await self.copy()
        self.assertEqual(await self.rows(STAYS), [
            {"player_tag": "#PP29L", "clan_tag": CLAN, "from_at": T * US, "to_at": (T + HOUR) * US,
             "last_observed_at": (T + HOUR) * US, "captures": 2, "end_basis": "last_capture"},
            {"player_tag": "#PP29L", "clan_tag": OTHER, "from_at": (T + 2 * HOUR) * US, "to_at": (T + 2 * HOUR) * US,
             "last_observed_at": (T + 2 * HOUR) * US, "captures": 1, "end_basis": "last_capture"},
            {"player_tag": "#PP29L", "clan_tag": CLAN, "from_at": (T + 3 * HOUR) * US, "to_at": (T + 5 * HOUR) * US,
             "last_observed_at": (T + 5 * HOUR) * US, "captures": 2, "end_basis": "last_capture"}])
        self.assertEqual(checks["captures"], {"captures": 6, "in_stays": 5, "two_clans": 1, "after_record_membership": 0})
        self.assertEqual(checks["stays"], 3)
        basis = await self.rows("SELECT DISTINCT basis,origin FROM old_clan_stays")
        self.assertEqual(basis, [{"basis": "observed_hourly", "origin": "legacy_health"}])

    async def test_a_gap_of_more_than_two_hours_splits_a_stay_even_back_in_the_same_clan(self):
        # Two hours apart is still hourly capturing with one missed; one second more is an absence.
        for seconds in (0, HOUR, 3 * HOUR, 6 * HOUR + 1):
            self.snap(T + seconds, "#PP29L")
        for seconds in (0, 2 * HOUR + 1):
            self.snap(T + seconds, "#PP29Q")
        await self.accounts("#PP29L", "#PP29Q")
        checks = await self.copy()
        self.assertEqual([(s["player_tag"], s["from_at"] // US - T, s["to_at"] // US - T, s["captures"])
                          for s in await self.rows(STAYS)], [
            ("#PP29L", 0, 3 * HOUR, 3), ("#PP29L", 6 * HOUR + 1, 6 * HOUR + 1, 1),
            ("#PP29Q", 0, 0, 1), ("#PP29Q", 2 * HOUR + 1, 2 * HOUR + 1, 1)])
        self.assertEqual(checks["captures"], {"captures": 6, "in_stays": 6, "two_clans": 0, "after_record_membership": 0})
        self.assertEqual(checks["stays"], 4)

    async def test_a_two_clan_capture_does_not_hide_a_gap(self):
        # Away from hour 0 to hour 5, seen in both clans at 5, back in one clan at 6: two stays, not one.
        self.snap(T, "#PP29L")
        self.snap(T + 5 * HOUR, "#PP29L")
        self.snap(T + 5 * HOUR, "#PP29L", "TWO")
        self.snap(T + 6 * HOUR, "#PP29L")
        await self.accounts("#PP29L")
        checks = await self.copy()
        self.assertEqual([(s["from_at"] // US - T, s["to_at"] // US - T, s["captures"]) for s in await self.rows(STAYS)],
                         [(0, 0, 1), (6 * HOUR, 6 * HOUR, 1)])
        self.assertEqual(checks["captures"], {"captures": 3, "in_stays": 2, "two_clans": 1, "after_record_membership": 0})

    async def test_a_stay_only_reaches_the_record_membership_without_a_gap_before_it(self):
        # Seen up to hour 1 and again after the record begins: joined only when the captures were hourly.
        for tag, later in (("#PP29L", 2 * HOUR), ("#PP29Q", 4 * HOUR)):
            self.snap(T, tag)
            self.snap(T + HOUR, tag)
            self.snap(T + later, tag)
            await self.membership(tag, (T + 2 * HOUR) * US - 1)
        checks = await self.copy()
        self.assertEqual([(s["player_tag"], s["to_at"], s["end_basis"]) for s in await self.rows(STAYS)], [
            ("#PP29L", (T + 2 * HOUR) * US - 1, "record_membership"), ("#PP29Q", (T + HOUR) * US, "last_capture")])
        self.assertEqual(checks["captures"], {"captures": 6, "in_stays": 4, "two_clans": 0, "after_record_membership": 2})
        self.assertEqual(checks["stays_ending_at_record"], 1)

    async def test_stays_end_where_the_record_membership_begins(self):
        for offset in range(4):
            self.snap(T + offset * HOUR, "#PP29L")
            self.snap(T + offset * HOUR, "#PP29Q", "TWO" if offset < 2 else "ONE")
        self.snap(T + 4 * HOUR, "#PP29Q", "TWO")
        cut = (T + 2 * HOUR) * US - 1
        await self.membership("#PP29L", cut)
        await self.membership("#PP29Q", cut)
        checks = await self.copy()
        stays = await self.rows(STAYS)
        # Still in the clan when the record begins: ends there. Moved at the cut: ends at its last capture.
        self.assertEqual([(s["player_tag"], s["clan_tag"], s["to_at"], s["end_basis"]) for s in stays], [
            ("#PP29L", CLAN, cut, "record_membership"), ("#PP29Q", OTHER, (T + HOUR) * US, "last_capture")])
        self.assertEqual(checks["captures"], {"captures": 9, "in_stays": 4, "two_clans": 0, "after_record_membership": 5})
        self.assertEqual(checks["stays_ending_at_record"], 1)

    async def test_record_membership_that_changes_after_the_read_refuses(self):
        self.snap(T, "#PP29L")
        await self.accounts("#PP29L")

        def operation(c):
            frozen = snapshot_for_record(c, self.health, CLANS)
            return frozen
        frozen = await self.store.call(operation)
        await self.membership("#PP29L", (T + HOUR) * US)
        before = await self.dump()

        def write(c):
            with sqlite_transaction(c, immediate=True):
                write_health(c, frozen)
        with self.assertRaisesRegex(ImportMismatch, "record_membership_changed"):
            await self.store.call(write)
        self.assertEqual(await self.dump(), before)

    async def test_capture_of_an_account_the_record_lacks_refuses(self):
        self.snap(T, "#PP29L")
        await self.refused("snapshot_account_missing")


class RaidCopyTests(HealthFixture):
    async def test_lines_match_fill_and_old_weekends_are_written_member_only(self):
        await self.accounts("#PP29L", "#PP29Q")
        await self.fetch(body(weekend(FIRST, [member("#PP29L", 6, 1000)])))
        self.raid(FIRST_END, "#PP29L")
        self.raid(FIRST_END, "#PP29Q", attacks=3, bonus=0, loot=400)
        self.raid(OLD_END, "#PP29G", attacks=2, loot=90)
        checks = await self.copy()
        self.assertEqual(checks["raid_lines"], {"matched": 1, "kept_record": 0, "left_running": 0, "imported": 2,
                                                "imported_record_weekends": 1, "imported_old_weekends": 1})
        self.assertEqual(checks["raid_weekends"], {"record": 1, "old_only": 1, "running": 0})
        self.assertEqual(checks["raid_differences"], [])
        self.assertEqual(checks["created_accounts"], 1)
        lines = await self.rows("SELECT l.*,a.player_tag AS owner,r.collector FROM raid_member_lines l "
                                "JOIN collection_runs r ON r.id=l.source_run_id "
                                "LEFT JOIN accounts a ON a.id=l.account_id ORDER BY l.weekend_start,l.player_tag")
        self.assertEqual([(r["weekend_start"], r["player_tag"], r["owner"], r["attacks"], r["bonus_attack_limit"],
                           r["capital_loot"], r["collector"]) for r in lines], [
            (STARTS[FIRST] - 7 * DAY * US, "#PP29G", "#PP29G", 2, 1, 90, "health_import"),
            (STARTS[FIRST], "#PP29L", "#PP29L", 6, 1, 1000, "capital_raids"),
            (STARTS[FIRST], "#PP29Q", "#PP29Q", 3, 0, 400, "health_import")])
        self.assertEqual(lines[0]["observed_at"], OLD_END * US)
        self.assertEqual(await self.rows("SELECT clan_tag,weekend_start,end_at,members,origin FROM old_raid_weekends"), [
            {"clan_tag": CLAN, "weekend_start": STARTS[FIRST] - 7 * DAY * US, "end_at": OLD_END * US, "members": 1,
             "origin": "legacy_health"}])
        self.assertEqual(await self.rows("SELECT tracking_status,first_observed_at FROM accounts WHERE player_tag='#PP29G'"),
                         [{"tracking_status": "legacy_raid", "first_observed_at": None}])
        # The record's weekend read now shows the filled member.
        window = await self.reads.window((CLAN,), after=STARTS[FIRST], before=STARTS[FIRST] + 1)
        self.assertEqual([m["player_tag"] for m in window["weekends"][0]["members"]], ["#PP29L", "#PP29Q"])

    async def test_an_ended_line_that_differs_keeps_the_record_and_lists_the_old_values(self):
        await self.fetch(body(weekend(FIRST, [member("#PP29L", 6, 1000)])))
        before = await self.rows("SELECT * FROM raid_member_lines")
        # The old loop's last capture came before the final attacks.
        self.raid(FIRST_END, "#PP29L", attacks=4, loot=700)
        checks = await self.copy()
        self.assertEqual(await self.rows("SELECT * FROM raid_member_lines"), before)
        self.assertEqual((checks["raid_lines"]["kept_record"], checks["raid_lines"]["imported"]), (1, 0))
        self.assertEqual(checks["raid_differences"], [{
            "clan_tag": CLAN, "weekend_start": STARTS[FIRST], "player_tag": "#PP29L", "reason": "record_line",
            "record": {"attacks": 6, "attack_limit": 5, "bonus_attack_limit": 1, "capital_loot": 1000},
            "old": {"attacks": 4, "attack_limit": 5, "bonus_attack_limit": 1, "capital_loot": 700}}])
        marker = await self.rows("SELECT checks_json FROM imports WHERE topic=?", (health_import.TOPIC,))
        self.assertEqual(json.loads(marker[0]["checks_json"])["raid_differences"], checks["raid_differences"])

    async def test_a_running_weekend_is_left_to_the_record(self):
        # Weekends that end after the copy reads the old file: one the record holds, one it has not seen yet.
        now = int(time.time())
        held_end, unseen_end = now + DAY, now + 2 * DAY
        held = {"state": "ongoing", "startTime": stamp(held_end - 3 * DAY), "endTime": stamp(held_end),
                "capitalTotalLoot": 5, "members": [member("#PP29L", 2, 200)]}
        await self.fetch(body(held))
        before = await self.rows("SELECT * FROM raid_member_lines")
        self.raid(held_end, "#PP29L", attacks=4, loot=400)
        self.raid(held_end, "#PP29Q", attacks=1, loot=100)
        self.raid(unseen_end, "#PP29G", attacks=3, loot=300)
        checks = await self.copy()
        self.assertEqual(await self.rows("SELECT * FROM raid_member_lines"), before)
        self.assertEqual(await self.rows("SELECT * FROM old_raid_weekends"), [])
        self.assertEqual(checks["raid_lines"], {"matched": 0, "kept_record": 1, "left_running": 2, "imported": 0,
                                                "imported_record_weekends": 0, "imported_old_weekends": 0})
        self.assertEqual(checks["raid_weekends"], {"record": 1, "old_only": 0, "running": 1})
        self.assertEqual([(d["player_tag"], d["reason"], d["record"] and d["record"]["attacks"], d["old"]["attacks"])
                          for d in checks["raid_differences"]],
                         [("#PP29L", "record_line", 2, 4), ("#PP29Q", "running_weekend", None, 1),
                          ("#PP29G", "running_weekend", None, 3)])
        # Nobody needs an account for a line the copy left out.
        self.assertEqual((checks["created_accounts"],
                          await self.rows("SELECT 1 FROM accounts WHERE tracking_status='legacy_raid'")), (0, []))

    async def test_a_weekend_id_that_contradicts_its_end_still_refuses_after_a_kept_line(self):
        await self.fetch(body(weekend(FIRST, [member("#PP29L", 6, 1000)])))
        self.raid(FIRST_END, "#PP29L", loot=1)
        self.raid(OLD_END, "#PP29Q", ident=weekend_id(CLAN, OLD_END + HOUR))
        await self.refused("bad_row:raid_member_activity")

    async def test_weekend_times_that_disagree_with_the_record_refuse(self):
        long = weekend(FIRST, [member("#PP29L")])
        long["endTime"] = "20261013T070000.000Z"
        await self.fetch(body(long))
        self.raid(FIRST_END, "#PP29Q")
        await self.refused("raid_weekend_end_mismatch")
        self.old("DELETE FROM raid_member_activity")
        self.raid(api_time(long["endTime"]) // US, "#PP29Q")
        await self.refused("raid_weekend_start_mismatch")

    async def test_bad_old_rows_refuse(self):
        await self.accounts("#PP29L")
        for change in ("weekend_id='RAID:#PP9:20261012T080000.000Z'", "clan_code='NONE'", "clan_tag='#PP8'",
                       "player_tag='bad'", "attacks=-1", "loot=1.5"):
            with self.subTest(change=change):
                self.old("DELETE FROM raid_member_activity")
                self.raid(FIRST_END, "#PP29L")
                self.old(f"UPDATE raid_member_activity SET {change}")
                await self.refused("bad_row:raid_member_activity")
        self.old("DELETE FROM raid_member_activity")
        self.snap(T, "#PP29L", "NONE")
        await self.refused("bad_row:player_snapshots")


class NameTests(HealthFixture):
    async def test_only_accounts_without_a_name_get_the_old_one(self):
        for offset, name in enumerate(("First", "Second", "Second")):
            self.snap(T + offset * HOUR, "#PP29L", name=name)
        self.snap(T, "#PP29Q", name="Same")
        self.snap(T, "#PP29G", name="Old spelling")
        self.directory("#PP29L", "Second")
        self.directory("#PP29Q", "Same")
        self.directory("#PP29G", "Old spelling")
        await self.accounts("#PP29L", "#PP29Q", "#PP29G", named={"#PP29Q": "Same", "#PP29G": "New spelling"})
        checks = await self.copy()
        self.assertEqual(checks["names"], {"equal": 1, "differing": 1, "added": 1})
        names = await self.rows("SELECT a.player_tag,a.current_name,n.name,n.from_at,i.source_kind FROM accounts a "
                                "JOIN account_aliases n ON n.account_id=a.id AND n.to_at IS NULL "
                                "JOIN collection_items i ON i.id=n.source_item_id ORDER BY a.player_tag")
        self.assertEqual([(r["player_tag"], r["current_name"], r["from_at"], r["source_kind"]) for r in names], [
            ("#PP29G", "New spelling", 1, "players"), ("#PP29L", "Second", (T + HOUR) * US, "health_import"),
            ("#PP29Q", "Same", 1, "players")])

    async def test_directory_that_disagrees_with_the_captures_refuses(self):
        self.snap(T, "#PP29L", name="Captured")
        self.directory("#PP29L", "Directory")
        await self.accounts("#PP29L")
        await self.refused("directory_name_mismatch")


class StartupTests(HealthFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ranked = self.path.parent / "ranked.db"
        with closing(sqlite3.connect(self.ranked)) as c, c:
            ranked_schema(c)
            c.execute("PRAGMA user_version=1")
        self.snap(T, "#PP29L")
        self.snap(T + HOUR, "#PP29L")
        self.directory("#PP29L")
        self.raid(OLD_END, "#PP29G")

    async def startup(self):
        return await self.store.call(wars_import.copy_at_startup, self.health, None, self.ranked, clans=CLANS)

    async def test_first_start_copies_after_profiles_and_a_second_start_reads_nothing(self):
        result = await self.startup()
        self.assertTrue(result["profiles"]["imported"])
        checks = result["health"]["checks"]
        self.assertEqual((checks["stays"], checks["names"]["added"], checks["raid_lines"]["imported"]), (1, 1, 1))
        marker = await self.rows("SELECT * FROM imports WHERE topic=?", (health_import.TOPIC,))
        self.assertEqual(json.loads(marker[0]["checks_json"]), checks)
        before = await self.dump()
        with patch.object(health_import, "snapshot_for_record", side_effect=AssertionError("source read")):
            self.assertIsNone(await self.startup())
        self.assertEqual(await self.dump(), before)

    async def test_start_with_earlier_copies_done_runs_only_this_part(self):
        await self.store.call(wars_import.copy_at_startup, self.health, None, clans=CLANS)

        def earlier(c):
            ranked = ranked_import.snapshot_ranked(self.ranked)
            profiles = profile_import.snapshot_for_record(c, self.health, self.ranked)
            with sqlite_transaction(c, immediate=True):
                ranked_import.write_ranked(c, ranked, ranked_import.plan_ranked(c, ranked))
                profile_import.write_profiles(c, profiles)
        await self.store.call(earlier)
        with patch.object(profile_import, "snapshot_for_record", side_effect=AssertionError("profile source read")):
            result = await self.startup()
        self.assertEqual([key for key, value in result.items() if value is not None], ["health"])
        self.assertEqual(result["health"]["checks"]["stays"], 1)

    async def test_forced_failure_keeps_nothing_from_any_part_and_the_next_start_copies(self):
        before = await self.dump()
        source = sha256(self.health.read_bytes()).digest()
        owner = FamilyCollectors(SimpleNamespace(boot_complete=asyncio.Event()), self.store, None, (),
                                 old_wars=(self.health, None, self.ranked))
        with patch.object(health_import, "verify_health", side_effect=ImportMismatch("forced_after_health_writes")), \
                patch.object(wars_import, "CLANS", {code: SimpleNamespace(tag=tag) for code, tag in CLANS.items()}), \
                self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
            await owner._copy_old_wars()
        self.assertEqual(await self.dump(), before)
        self.assertEqual(sha256(self.health.read_bytes()).digest(), source)
        with patch.object(wars_import, "CLANS", {code: SimpleNamespace(tag=tag) for code, tag in CLANS.items()}), \
                self.assertLogs("elbow_helper.data.collectors.service", level="INFO") as logs:
            await owner._copy_old_wars()
        self.assertIn("1 old raid lines, 1 old clan stays, 1 old names, 1 raid-only accounts", "\n".join(logs.output))

    async def test_upgrade_from_the_previous_version_adds_empty_tables(self):
        path = self.path.parent / "v14.sqlite3"
        with closing(sqlite3.connect(path)) as c:
            c.execute("PRAGMA foreign_keys=ON")
            run_sqlite_migrations(c, MIGRATIONS[:14], target_version=14)
        upgraded = FamilyStore(path)
        await upgraded.initialize()
        self.assertEqual(await upgraded.call(lambda c: [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                                                        for t in ("old_clan_stays", "old_raid_weekends")]), [0, 0])
