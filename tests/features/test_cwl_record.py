"""League history and league groups read from synthetic family-record weeks.

Collector weeks use the collector's own functions; historical weeks are
seeded directly in the record.
"""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.reads import CwlReads, WarReads
from elbow_helper.data.store.api_fields import collect_fields
from elbow_helper.data.store.link_state import receipt
from elbow_helper.features.cwl.record import CwlGroups, CwlHistory, league_wars
from elbow_helper.infrastructure.persistence.revisions import release_sqlite_revision

from tests.data.test_wars import SECOND, group, war
from tests.data.record_fixtures import CwlFixture

DAY = 86400 * SECOND
CLANS = {"ONE": SimpleNamespace(tag="#PY"), "TWO": SimpleNamespace(tag="#LQ")}
# Round 1's battle day of a synthetic November league.
NOV = int(datetime(2026, 11, 2, 8, tzinfo=timezone.utc).timestamp()) * SECOND


def clash(at):
    return datetime.fromtimestamp(at // SECOND, timezone.utc).strftime("%Y%m%dT%H%M%S.000Z")


def league_war(state, round_no, *, first=NOV, clans=("#PY", "#GR")):
    """One league war of round ``round_no``: one attack each, the second member attacks only once it ended."""
    payload = war(state, clans=clans)
    start = first + (round_no - 1) * DAY
    payload.update(attacksPerMember=1, preparationStartTime=clash(start - DAY), startTime=clash(start),
                   endTime=clash(start + DAY))
    return payload


def swapped(payload, old="#P2", new="#P8"):
    """The same war with ``new`` in ``old``'s place everywhere."""
    return json.loads(json.dumps(payload).replace(f'"{old}"', f'"{new}"').replace(f"Player {old}", f"Player {new}"))


def end_of(round_no, first=NOV):
    return first + round_no * DAY


@contextmanager
def now(at):
    with patch("elbow_helper.data.reads.core.now_us", return_value=at):
        yield


class LeagueFixture(CwlFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reads = CwlReads(self.store, CLANS)
        self.history = CwlHistory(self.reads, None, CLANS)
        self.groups = CwlGroups(self.reads, CLANS)

    async def call(self, method, *args, at=None, **kwargs):
        with now(at if at is not None else NOV + 30 * DAY):
            return await asyncio.to_thread(method, *args, **kwargs)

    async def november(self, *, first=NOV, season="2026-11"):
        """BE-style week of clan #PY: round 1 ended, round 2 in battle day, round 3 in preparation."""
        rounds = [{"warTags": ["#R0", "#R2"]}, {"warTags": ["#R8", "#R9"]}, {"warTags": ["#RC", "#RJ"]}]
        await self.record(group(rounds=rounds, season=season), kind="cwl_group", at=first - 2 * DAY)
        await self.record(league_war("warEnded", 1, first=first), kind="cwl_war", scope="#R0",
                          at=end_of(1, first) + 90 * SECOND)
        await self.record(league_war("inWar", 2, first=first), kind="cwl_war", scope="#R8",
                          at=end_of(2, first) - 3600 * SECOND)
        await self.record(league_war("preparation", 3, first=first), kind="cwl_war", scope="#RC",
                          at=end_of(2, first) - 3600 * SECOND)

    async def league_on_profile(self, name, at, tag="#PY"):
        def write(c):
            run = c.execute("SELECT run_id FROM collection_items WHERE id=?",
                            (receipt(c, "test", "test", 0, 0),)).fetchone()[0]
            collect_fields(c, "clan_profile", tag, {"tag": tag, "warLeague": {"id": 1, "name": name}}, run, at)
            c.commit()
        await self.store.call(write)


class HistoryTests(LeagueFixture, unittest.IsolatedAsyncioTestCase):


    async def test_a_league_takes_the_month_the_collector_stored_whatever_day_it_starts(self):
        await self.november(first=int(datetime(2026, 11, 17, 8, tzinfo=timezone.utc).timestamp()) * SECOND)
        self.assertEqual(await self.call(self.history.bonus_seasons, ["ONE"], at=NOV + 60 * DAY), ["2026-11"])
        with now(NOV + 60 * DAY):
            self.assertEqual({war["_season"] for war in await self.groups.wars("ONE")}, {"2026-11"})

    async def assert_league(self, league, at):
        weeks = await self.reads.weeks(["#PY"], ended=False, as_of=at)
        self.assertEqual([week["league_name"] for week in weeks["weeks"]], [league])
        current = await self.reads.group("#PY", as_of=at)
        self.assertEqual(current["league"], league)
        wars = await self.call(self.history.bonus_wars, "ONE", "2026-11", at=at)
        self.assertEqual({war["cwl_league"] for war in wars}, {league or ""})
        with now(at):
            self.assertEqual({war["_league"] for war in await self.groups.wars("ONE")}, {league})

    async def finish_november(self):
        for round_no, tag in ((2, "#R8"), (3, "#RC")):
            await self.record(league_war("warEnded", round_no), kind="cwl_war", scope=tag,
                              at=end_of(3) + SECOND)

    async def test_running_season_uses_the_latest_profile_at_the_earliest_war_start(self):
        await self.league_on_profile("Crystal League II", NOV - 3 * DAY)
        await self.league_on_profile("Crystal League I", NOV)
        await self.november()
        await self.assert_league("Crystal League I", end_of(2) - 3600 * SECOND)
        # Selecting a later round still uses the season's first start, not the selection's.
        await self.league_on_profile("Master League III", NOV + DAY)
        selected = await self.reads.weeks(["#PY"], war_tags=["#R8"], ended=False, as_of=end_of(3))
        self.assertEqual(selected["weeks"][0]["league_name"], "Crystal League I")
        await self.assert_league("Crystal League I", end_of(3))


    async def test_promotion_after_the_season_does_not_rename_it_even_without_final_captures(self):
        await self.league_on_profile("Crystal League I", NOV - 3 * DAY)
        await self.november()
        await self.assert_league("Crystal League I", end_of(3))
        await self.league_on_profile("Master League III", end_of(3) + SECOND)
        await self.assert_league("Crystal League I", end_of(3) + DAY)
        await self.finish_november()
        await self.assert_league("Crystal League I", end_of(3) + DAY)
        fresh = CwlReads(self.store, CLANS)
        weeks = await fresh.weeks(["#PY"], as_of=end_of(3) + DAY)
        self.assertEqual(weeks["weeks"][0]["league_name"], "Crystal League I")
        self.assertEqual((await fresh.group("#PY", as_of=end_of(3) + DAY))["league"], "Crystal League I")

    async def test_no_profile_before_the_season_leaves_the_league_unknown(self):
        await self.november()
        await self.assert_league(None, end_of(3))
        # A profile first saved during the season cannot establish its starting league.
        await self.league_on_profile("Crystal League I", NOV + SECOND)
        await self.assert_league(None, end_of(3))
        await self.finish_november()
        await self.assert_league(None, end_of(3) + DAY)

    async def test_starting_league_is_reread_when_profile_history_is_corrected(self):
        await self.league_on_profile("Crystal League II", NOV - 3 * DAY)
        await self.november()
        await self.finish_november()
        await self.assert_league("Crystal League II", end_of(3) + DAY)
        await self.league_on_profile("Crystal League I", NOV - DAY)
        await self.assert_league("Crystal League I", end_of(3) + DAY)


    async def test_roster_history_keeps_the_newest_complete_seasons_per_clan(self):
        await self.week(season="2026-08", first_end=1786000000, prefix="#GG")
        await self.week(season="2026-09", first_end=1788700000, prefix="#JJ", opponents=("#U0", "#U2", "#U8"))
        await self.week(code="TWO", tag="#LQ", season="2026-09", first_end=1788700000, prefix="#CC",
                        opponents=("#V0", "#V2", "#V8"), lineup=("#Q8", "#Q9"))
        with patch("elbow_helper.features.cwl.record.SEASON_WARS", 3):
            latest = await self.call(self.history.roster_history, 1)
            scoped = await self.call(self.history.roster_history, 3, season="2026-08", clan_code="ONE")
        self.assertEqual(latest["seasons"], [{"key": "2026-09", "latest_end_ts": 1788700000 + 2 * 86400}])
        self.assertEqual({(w["clan_code"], w["cwl_season"]) for w in latest["wars"]}, {("ONE", "2026-09"),
                                                                                       ("TWO", "2026-09")})
        self.assertEqual([w["end_ts"] for w in latest["wars"]], sorted(w["end_ts"] for w in latest["wars"]))
        self.assertEqual([s["key"] for s in scoped["seasons"]], ["2026-08"])

    async def test_chosen_wars_are_exactly_the_clans_ended_wars_and_others_are_refused_with_why(self):
        await self.november()
        await self.week(code="TWO", tag="#LQ", prefix="#CC", opponents=("#V0", "#V2", "#V8"), lineup=("#Q8", "#Q9"))
        chosen = await self.call(self.history.scoring_wars, "ONE", ["#R0"])
        self.assertEqual([w["war_id"] for w in chosen], ["CWL:#R0"])
        self.assertEqual(len(chosen[0]["roster"]), 2)
        with self.assertRaises(ValueError) as refused:
            await self.call(self.history.scoring_wars, "ONE", ["#R0", "#NOPE", "#CC0", "#R8"])
        self.assertEqual(str(refused.exception), "Unknown CWL war IDs: #NOPE; Wars from another clan than ONE: "
                                                 "CWL:#CC0; Unfinished CWL wars: CWL:#R8.")
        # An old ID names the same war.
        self.assertEqual([w["war_id"] for w in await self.call(self.history.scoring_wars, "TWO", ["CWL:#CC0"])],
                         ["CWL:#CC0"])


    async def test_names_come_from_both_lineups_and_the_directory_and_revisions_follow_writes(self):
        self.addCleanup(release_sqlite_revision, self.path)
        await self.november()
        directory = SimpleNamespace(directory_names=lambda: [{"player_tag": "#P9", "player_name": "Former"}],
                                    path=self.path)
        history = CwlHistory(self.reads, directory, CLANS)
        names = await self.call(history.account_names)
        self.assertEqual(names, [{"player_tag": "#P0", "player_name": "Player #P0"},
                                 {"player_tag": "#P2", "player_name": "Player #P2"},
                                 {"player_tag": "#P9", "player_name": "Former"}])
        before = history.cache_revision()
        self.assertTrue(before)
        await self.league_on_profile("Crystal League I", NOV)
        self.assertNotEqual(history.cache_revision(), before)

    async def attendance_matches_shared_rows(self):
        result = await self.call(self.history.attendance, clan_codes=["ONE"], war_type="CWL")
        shared = await WarReads(self.store).participation(
            ["#PY"], NOV - DAY, NOV + 30 * DAY, types=("CWL",), as_of=NOV + 30 * DAY)
        by_tag = {row["player_tag"]: row for row in shared["rows"]}
        for row in result["rows"]:
            expected = by_tag[row["player_tag"]]
            self.assertEqual((row["attacks_used"], row["missed"], row["final_capture"]),
                             (expected["attacks_used"], expected["missed"], expected["missed"] is not None))
        return result

    async def test_missing_saved_attack_keeps_used_counts_and_unproven_misses_unknown(self):
        payload = league_war("warEnded", 1)
        payload["teamSize"] = 3
        for side, tag in (("clan", "#P8"), ("opponent", "#Q8")):
            payload[side]["members"].append({"tag": tag, "name": f"Player {tag}", "townhallLevel": 16,
                                              "mapPosition": 3, "opponentAttacks": 0})
        await self.record(group(rounds=[{"warTags": ["#R0", "#R2"]}], season="2026-11"), kind="cwl_group",
                          at=NOV - 2 * DAY)
        await self.record(payload, kind="cwl_war", scope="#R0", at=end_of(1) + 90 * SECOND)
        # A copied total is no independent proof of final counts. #P2's saved
        # attack goes missing, leaving #P8's zero unproven.
        await self.execute("UPDATE war_sides SET extra_json=? WHERE war_id=(SELECT id FROM wars WHERE war_tag='#R0') "
                           "AND clan_tag='#PY'", (json.dumps({"derived": ["attacks"]}),))
        await self.execute("DELETE FROM war_attacks WHERE attacker_member_id IN (SELECT m.id FROM war_members m "
                           "JOIN wars w ON w.id=m.war_id WHERE w.war_tag='#R0' AND m.player_tag='#P2')")
        result = await self.attendance_matches_shared_rows()
        rows = {row["player_tag"]: row for row in result["rows"]}
        self.assertEqual((rows["#P2"]["attacks_used"], rows["#P2"]["missed"]), (1, 0))
        self.assertEqual(rows["#P2"]["observed_attack_performance"]["attacks"], 0)
        self.assertIsNone(rows["#P8"]["missed"])
        self.assertIsNone(rows["#P8"]["attacks_expected"])
        self.assertFalse(rows["#P8"]["final_capture"])
        self.assertEqual(rows["#P8"]["uncertainty_reasons"], ["attacks_incomplete"])
        self.assertEqual(result["unproven_war_ids"], ["CWL:#R0"])
        self.assertFalse(result["coverage"]["exact_ranking"])
        (coverage,) = result["coverage"]["wars"]
        self.assertEqual((coverage["war_id"], coverage["own"]["attacks_stored"],
                          coverage["own"]["attacks_reported"]), ("CWL:#R0", 1, 2))
        self.assertFalse(coverage["own"]["attacks_complete"])
        (bonus,) = await self.call(self.history.bonus_wars, "ONE", "2026-11")
        self.assertEqual(bonus["coverage"]["own"], coverage["own"])
        with patch("elbow_helper.features.cwl.record.SEASON_WARS", 1):
            history = await self.call(self.history.roster_history, None)
        self.assertEqual(history["wars"][0]["coverage"]["own"], coverage["own"])
        with now(NOV + 30 * DAY):
            summary = await self.reads.weeks(["#PY"], details=False)
        self.assertEqual(summary["weeks"][0]["wars"][0]["coverage"]["own"], coverage["own"])

    async def test_final_reported_counts_prove_misses_even_without_saved_attacks(self):
        payload = league_war("inWar", 1)
        payload["state"] = "warEnded"
        await self.record(group(rounds=[{"warTags": ["#R0", "#R2"]}], season="2026-11"), kind="cwl_group",
                          at=NOV - 2 * DAY)
        await self.record(payload, kind="cwl_war", scope="#R0", at=end_of(1) + 90 * SECOND)
        for missing_attacks in (False, True):
            with self.subTest(missing_attacks=missing_attacks):
                if missing_attacks:
                    await self.execute("DELETE FROM war_attacks")
                result = await self.attendance_matches_shared_rows()
                self.assertEqual([(row["player_tag"], row["attacks_used"], row["missed"], row["final_capture"])
                                  for row in result["rows"]], [("#P0", 1, 0, True), ("#P2", 0, 1, True)])
                self.assertEqual(result["unproven_war_ids"], [])
                self.assertTrue(result["coverage"]["exact_ranking"])


    async def test_a_chosen_season_or_window_lists_running_wars_with_no_misses(self):
        await self.november()
        chosen = {"clan_codes": ["ONE"], "war_type": "CWL", "running": True}
        result = await self.call(self.history.attendance, **chosen, season="2026-11")
        self.assertEqual(result["war_ids"], ["CWL:#R0", "CWL:#R8", "CWL:#RC"])
        self.assertEqual(result["coverage"]["wars_not_final"], 2)
        rows = {(row["war_id"], row["player_tag"]): row for row in result["rows"]}
        running = rows["CWL:#R8", "#P0"]
        self.assertEqual((running["attacks_used"], running["missed"], running["final_capture"],
                          running["observed_attack_performance"]["attacks"]), (1, None, False, 1))
        self.assertIn("not_final", running["uncertainty_reasons"])
        self.assertEqual((rows["CWL:#RC", "#P0"]["attacks_used"], rows["CWL:#RC", "#P0"]["missed"]), (0, None))
        self.assertIn("CWL:#R8", result["unproven_war_ids"])
        self.assertFalse(result["coverage"]["exact_ranking"])
        # A window holds a running war once it started, whenever it will end.
        window = await self.call(self.history.attendance, **chosen, after_ts=(NOV - 2 * DAY) // SECOND,
                                 before_ts=(end_of(2) - DAY // 2) // SECOND)
        self.assertEqual(window["war_ids"], ["CWL:#R0", "CWL:#R8"])
        # By default the same selection stays completed wars.
        result = await self.call(self.history.attendance, clan_codes=["ONE"], war_type="CWL", season="2026-11")
        self.assertEqual((result["war_ids"], result["coverage"]["wars_not_final"]), (["CWL:#R0"], 0))


    async def test_an_unreadable_record_fails_the_read_instead_of_returning_nothing(self):
        broken = SimpleNamespace(weeks=AsyncMock(side_effect=sqlite3.OperationalError("locked")))
        with self.assertRaises(sqlite3.OperationalError):
            await asyncio.to_thread(CwlHistory(broken, None, CLANS).bonus_wars, "ONE", "2026-11")
        with self.assertRaises(sqlite3.OperationalError):
            await asyncio.to_thread(CwlHistory(broken, None, CLANS).attendance, clan_codes=["ONE"], war_type="CWL")


class GroupTests(LeagueFixture, unittest.IsolatedAsyncioTestCase):
    async def wars(self, at):
        with now(at):
            return await self.groups.wars("ONE")


    async def test_a_war_whose_checks_stopped_is_stale_and_an_ended_war_never_is(self):
        await self.november()
        wars = await self.wars(end_of(2) + 2 * 3600 * SECOND)
        self.assertEqual([w["_snapshot_stale"] for w in wars], [False, True, True])
        with now(end_of(2) + 2 * 3600 * SECOND):
            usable, warnings = await self.groups.current_wars("ONE")
        self.assertEqual([w["_warTag"] for w in usable], ["#R0"])
        self.assertEqual(warnings, ["ONE: 2 CWL wars couldn't be loaded."])


    async def test_spin_status_needs_a_recent_check_to_say_not_started(self):
        day1 = int(datetime(2026, 11, 1, 9, tzinfo=timezone.utc).timestamp()) * SECOND
        moment = datetime.fromtimestamp(day1 // SECOND + 60, timezone.utc)

        async def status(at, when=moment):
            with now(at):
                return await self.groups.spin_status("ONE", when)

        self.assertEqual(await status(day1), "unavailable")
        await self.record({"state": "notInWar"}, kind="cwl_group", at=day1)
        self.assertEqual(await status(day1 + 60 * SECOND), "not_started")
        # Overdue during the league days: a league may have started since.
        late = day1 + 20 * 60 * SECOND
        self.assertEqual(await status(late, datetime.fromtimestamp(late // SECOND, timezone.utc)), "unavailable")
        # Outside the league days no league is starting: no check is needed, or read.
        day20 = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp()) * SECOND
        self.assertEqual(await status(day20, datetime.fromtimestamp(day20 // SECOND, timezone.utc)), "not_started")
        broken = CwlGroups(SimpleNamespace(group=AsyncMock(side_effect=AssertionError("read"))), CLANS)
        self.assertEqual(await broken.spin_status("ONE", datetime(2026, 11, 12, tzinfo=timezone.utc)), "not_started")

    async def test_spin_status_follows_the_leagues_own_season(self):
        await self.november()
        at = end_of(1) + 3600 * SECOND
        november = datetime(2026, 11, 3, tzinfo=timezone.utc)
        with now(at):
            self.assertEqual(await self.groups.spin_status("ONE", november), "started")
            # Last month's league seen in a fresh check: this month's has not started.
            self.assertEqual(await self.groups.spin_status("ONE", datetime(2026, 12, 1, tzinfo=timezone.utc)),
                             "unavailable")
        await self.execute("UPDATE war_collection_state SET last_attempt_at=?,last_success_at=?,next_due_at=? "
                           "WHERE source_kind='cwl_group'", (at, at, at + 300 * SECOND))
        with now(at + 60 * SECOND):
            self.assertEqual(await self.groups.spin_status("ONE", datetime(2026, 12, 1, tzinfo=timezone.utc)),
                             "not_started")

    async def test_league_wars_keep_the_group_rounds_including_undrawn_ones(self):
        rounds = [{"warTags": ["#R0", "#R2"]}, {"warTags": ["#0"]}, {"warTags": ["#0"]}]
        await self.record(group(rounds=rounds, season="2026-11"), kind="cwl_group", at=NOV - 2 * DAY)
        await self.record(league_war("inWar", 1), kind="cwl_war", scope="#R0", at=NOV + 3600 * SECOND)
        with now(NOV + 3600 * SECOND + 30 * SECOND):
            group_view = await self.reads.group("#PY")
        self.assertEqual([w["_total_rounds"] for w in league_wars(group_view)], [3])


if __name__ == "__main__":
    unittest.main()
