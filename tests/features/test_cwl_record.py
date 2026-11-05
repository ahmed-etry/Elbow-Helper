"""League history and league groups read from synthetic family-record weeks.

Collector weeks are saved with the collector's own functions; copied weeks go
through the startup copy of a synthetic old store.
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
from elbow_helper.features.cwl.threads.snapshots import build_cwl_thread_snapshot
from elbow_helper.features.cwl.war_board import cwl_board_snapshot_is_complete, orient_cwl_war, select_cwl_board_wars
from elbow_helper.features.wars.emojis import EMPTY_WAR_EMOJIS
from elbow_helper.infrastructure.persistence.revisions import release_sqlite_revision
from elbow_helper.features.wars.rendering import build_war_board_embed

from tests.data.test_wars import SECOND, group, war
from tests.data.test_wars_import_cwl import CwlFixture

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
    async def test_collector_week_reads_in_the_old_bonus_shape_with_ended_wars_only(self):
        await self.november()
        self.assertEqual(await self.call(self.history.bonus_seasons, ["ONE"]), ["2026-11"])
        self.assertEqual(await self.call(self.history.bonus_seasons, ["TWO"]), [])
        (ended,) = await self.call(self.history.bonus_wars, "ONE", "2026-11")
        self.assertEqual({key: ended[key] for key in ("war_id", "war_type", "clan_code", "cwl_season", "cwl_round",
                                                      "team_size", "attacks_per_member", "state", "end_ts")},
                         {"war_id": "CWL:#R0", "war_type": "CWL", "clan_code": "ONE", "cwl_season": "2026-11",
                          "cwl_round": 1, "team_size": 2, "attacks_per_member": 1, "state": "warEnded",
                          "end_ts": end_of(1) // SECOND})
        self.assertEqual([(r["player_tag"], r["map_position"], r["townhall"], r["attacks_expected"], r["attacks_used"])
                          for r in ended["roster"]], [("#P0", 1, 16, 1, 1), ("#P2", 2, 16, 1, 1)])
        self.assertEqual([(a["player_tag"], a["defender_tag"], a["defender_townhall"], a["defender_map_position"],
                           a["stars"]) for a in ended["attacks"]], [("#P0", "#Q0", 16, 1, 3), ("#P2", "#Q2", 16, 2, 3)])
        # Exactly the fields the bonus and scoring code always read.
        self.assertEqual(set(ended["attacks"][0]), {"war_id", "player_tag", "player_name", "attack_order",
                                                    "defender_tag", "defender_townhall", "defender_map_position",
                                                    "stars", "destruction"})
        self.assertEqual(await self.call(self.history.bonus_wars, "ONE", "2026-10"), [])

    async def test_league_positions_are_renumbered_as_the_lineup(self):
        payload = league_war("warEnded", 1)
        for side in ("clan", "opponent"):
            for member, position in zip(payload[side]["members"], (31, 44)):
                member["mapPosition"] = position
        await self.record(group(rounds=[{"warTags": ["#R0", "#R2"]}], season="2026-11"), kind="cwl_group",
                          at=NOV - 2 * DAY)
        await self.record(payload, kind="cwl_war", scope="#R0", at=end_of(1) + 90 * SECOND)
        (ended,) = await self.call(self.history.bonus_wars, "ONE", "2026-11")
        self.assertEqual([r["map_position"] for r in ended["roster"]], [1, 2])
        self.assertEqual([a["defender_map_position"] for a in ended["attacks"]], [1, 2])

    async def test_a_league_takes_the_month_the_collector_stored_whatever_day_it_starts(self):
        await self.november(first=int(datetime(2026, 11, 17, 8, tzinfo=timezone.utc).timestamp()) * SECOND)
        self.assertEqual(await self.call(self.history.bonus_seasons, ["ONE"], at=NOV + 60 * DAY), ["2026-11"])
        with now(NOV + 60 * DAY):
            self.assertEqual({war["_season"] for war in await self.groups.wars("ONE")}, {"2026-11"})

    async def test_league_name_is_the_clans_war_league_while_the_week_ran(self):
        await self.league_on_profile("Crystal League I", NOV - 3 * DAY)
        await self.november()
        week = lambda: self.call(self.history.bonus_wars, "ONE", "2026-11", at=end_of(3) + DAY)
        self.assertEqual((await week())[0]["cwl_league"], "Crystal League I")
        # A promotion after the week ended does not rename it; while it runs, the current league counts.
        await self.league_on_profile("Master League III", end_of(3) + 12 * 3600 * SECOND)
        self.assertEqual((await week())[0]["cwl_league"], "Master League III")
        for tag in ("#R8", "#RC"):
            await self.record(league_war("warEnded", 2 if tag == "#R8" else 3), kind="cwl_war", scope=tag,
                              at=end_of(3) + 13 * 3600 * SECOND)
        fresh = CwlHistory(CwlReads(self.store, CLANS), None, CLANS)
        wars = await self.call(fresh.bonus_wars, "ONE", "2026-11", at=end_of(3) + DAY)
        self.assertEqual({w["cwl_league"] for w in wars}, {"Crystal League I"})
        # Without any profile observation the league stays unknown, as old weeks without one were.
        await self.execute("DELETE FROM api_field_changes")
        await self.execute("DELETE FROM api_records")
        other = CwlHistory(CwlReads(self.store, CLANS), None, CLANS)
        self.assertEqual({w["cwl_league"] for w in await self.call(other.bonus_wars, "ONE", "2026-11")}, {""})

    async def test_copied_weeks_keep_their_old_key_league_positions_and_final_lineup(self):
        await self.week(season="2026-06-catchup", label="2026-06-16", positions=(30, 40), defender_positions=(31, 41))
        await self.run_cwl()
        self.assertEqual(await self.call(self.history.bonus_seasons, None), ["2026-06-catchup"])
        wars = await self.call(self.history.bonus_wars, "ONE", "2026-06-catchup")
        self.assertEqual([w["war_id"] for w in wars], ["CWL:#RR0", "CWL:#RR2", "CWL:#RR8"])
        self.assertEqual({w["cwl_league"] for w in wars}, {"Crystal League I"})
        # The old store's positions stay; the player swapped out before battle day is not in the lineup.
        self.assertEqual([[(r["player_tag"], r["map_position"]) for r in w["roster"]] for w in wars][0],
                         [("#P0", 30), ("#P2", 40)])
        self.assertEqual([a["defender_map_position"] for a in wars[0]["attacks"]], [31, 41])
        self.assertEqual([(r["attacks_used"]) for r in wars[1]["roster"]], [1, 0])
        history = await self.call(self.history.roster_history, None)
        self.assertEqual(history, {"seasons": [], "wars": [], "roster": [], "attacks": []})
        with patch("elbow_helper.features.cwl.record.SEASON_WARS", 3):
            history = await self.call(self.history.roster_history, None)
        self.assertEqual([s["key"] for s in history["seasons"]], ["2026-06-catchup"])
        self.assertEqual((len(history["wars"]), len(history["roster"]), len(history["attacks"])), (3, 6, 5))
        self.assertEqual(history["wars"][0]["cwl_season_label"], "2026-06-16")
        self.assertEqual(history["wars"][0]["source"], "war_cwl_api")
        self.assertTrue(all(attack["fresh_attack"] == 1 for attack in history["attacks"]))

    async def test_roster_history_keeps_the_newest_complete_seasons_per_clan(self):
        await self.week(season="2026-08", first_end=1786000000, prefix="#GG")
        await self.week(season="2026-09", first_end=1788700000, prefix="#JJ", opponents=("#U0", "#U2", "#U8"))
        await self.week(code="TWO", tag="#LQ", season="2026-09", first_end=1788700000, prefix="#CC",
                        opponents=("#V0", "#V2", "#V8"), lineup=("#Q8", "#Q9"))
        await self.run_cwl()
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
        await self.run_cwl()
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

    async def test_attendance_counts_final_lineups_and_selects_like_the_old_snapshot(self):
        await self.week()
        await self.november()
        await self.run_cwl()
        result = await self.call(self.history.attendance, clan_codes=["ONE"], war_type="CWL", season="2026-10")
        self.assertEqual(result["war_ids"], ["CWL:#RR0", "CWL:#RR2", "CWL:#RR8"])
        self.assertEqual(sorted((r["war_id"], r["player_tag"], r["missed"]) for r in result["rows"])[:4],
                         [("CWL:#RR0", "#P0", 0), ("CWL:#RR0", "#P2", 0), ("CWL:#RR2", "#P0", 0),
                          ("CWL:#RR2", "#P2", 1)])
        self.assertNotIn("#P8", {r["player_tag"] for r in result["rows"]})
        self.assertEqual(result["rows"][0]["observed_attack_performance"]["stars"], 3)
        self.assertEqual(result["war_aliases"][0]["sources"], ["war_cwl_api"])
        latest = await self.call(self.history.attendance, clan_codes=["ONE"], war_type="CWL", latest_completed=1)
        self.assertEqual(latest["war_ids"], ["CWL:#R0"])
        self.assertEqual(latest["war_aliases"][0]["sources"], ["family_record"])
        picked = await self.call(self.history.attendance, clan_codes=["ONE"], war_type="CWL", war_ids=["CWL:#RR8"])
        self.assertEqual(picked["war_ids"], ["CWL:#RR8"])
        with patch("elbow_helper.features.cwl.record.SEASON_WARS", 3):
            recent = await self.call(self.history.attendance, clan_codes=["ONE"], war_type="CWL", history_limit=1)
        self.assertEqual((recent["component_seasons"], recent["clan_seasons"]),
                         (["2026-10"], [{"season": "2026-10", "clan_code": "ONE", "wars": 3}]))
        for selection, message in (({"war_type": "REG"}, "league wars only"),
                                   ({"war_type": "CWL", "history_limit": 13}, "Invalid CWL history limit"),
                                   ({"war_type": "CWL", "history_limit": 1, "season": "2026-10"}, "not both")):
            with self.subTest(selection=selection), self.assertRaisesRegex(ValueError, message):
                await self.call(self.history.attendance, clan_codes=["ONE"], **selection)

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

    async def test_running_war_has_no_misses_and_is_excluded_from_completed_attendance(self):
        await self.november()
        with now(NOV + 30 * DAY):
            weeks = await self.reads.weeks(["#PY"], ended=False)
        running = next(war for week in weeks["weeks"] for war in week["wars"] if war["state"] == "inWar")
        self.assertTrue(all(member["missed"] is None for member in running["participation"]))
        self.assertEqual([member["attacks_used"] for member in running["participation"]], [1, 0])
        self.assertEqual(running["coverage"]["state"], "inWar")
        result = await self.attendance_matches_shared_rows()
        self.assertEqual(result["war_ids"], ["CWL:#R0"])

    async def test_unknown_used_count_stays_unknown_despite_a_saved_attack(self):
        await self.november()
        await self.execute("UPDATE war_members SET attacks_used=NULL WHERE player_tag='#P0' "
                           "AND war_id=(SELECT id FROM wars WHERE war_tag='#R0')")
        result = await self.attendance_matches_shared_rows()
        row = next(row for row in result["rows"] if row["player_tag"] == "#P0")
        self.assertIsNone(row["attacks_used"])
        self.assertIsNone(row["missed"])
        self.assertFalse(row["final_capture"])

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

    async def test_the_group_becomes_league_payloads_with_round_state_and_season(self):
        await self.november()
        at = end_of(2) - 3600 * SECOND + 30 * SECOND
        wars = await self.wars(at)
        self.assertEqual([(w["_warTag"], w["_round"], w["_state"], w["_total_rounds"], w["_season"],
                           w["_snapshot_stale"]) for w in wars],
                         [("#R0", 1, "warEnded", 3, "2026-11", False), ("#R8", 2, "inWar", 3, "2026-11", False),
                          ("#RC", 3, "preparation", 3, "2026-11", False)])
        battle = wars[1]
        self.assertEqual((battle["clan"]["tag"], battle["opponent"]["tag"], battle["teamSize"], battle["clan"]["attacks"]),
                         ("#PY", "#GR", 2, 1))
        self.assertEqual([len(m.get("attacks", [])) for m in battle["clan"]["members"]], [1, 0])
        self.assertEqual(battle["_coverage"]["state"], "inWar")
        # The boards and the thread status read these payloads unchanged.
        self.assertTrue(cwl_board_snapshot_is_complete(wars))
        selected = select_cwl_board_wars(wars)
        self.assertEqual([w["_warTag"] for w in selected], ["#R8", "#RC"])
        embeds = [build_war_board_embed(orient_cwl_war(w, "#PY"), EMPTY_WAR_EMOJIS) for w in selected]
        self.assertEqual(len(embeds), 2)
        snapshot = build_cwl_thread_snapshot(wars, "#PY", now=datetime.fromtimestamp(at // SECOND, timezone.utc))
        self.assertEqual((snapshot.battle.round_number, snapshot.battle.attacks_used, snapshot.battle.attacks_total,
                          snapshot.battle.missing_attacks), (2, 1, 2, ("Player #P2",)))
        self.assertEqual(snapshot.preparation.war_tag, "#RC")

    async def test_a_war_whose_checks_stopped_is_stale_and_an_ended_war_never_is(self):
        await self.november()
        wars = await self.wars(end_of(2) + 2 * 3600 * SECOND)
        self.assertEqual([w["_snapshot_stale"] for w in wars], [False, True, True])
        with now(end_of(2) + 2 * 3600 * SECOND):
            usable, warnings = await self.groups.current_wars("ONE")
        self.assertEqual([w["_warTag"] for w in usable], ["#R0"])
        self.assertEqual(warnings, ["ONE: 2 CWL wars couldn't be loaded."])

    async def test_no_group_or_an_unreadable_record_gives_no_wars_and_the_old_warnings(self):
        self.assertEqual(await self.wars(NOV), [])
        with now(NOV):
            self.assertEqual(await self.groups.current_wars("ONE"), ([], ["ONE: failed to fetch league group."]))
            self.assertEqual(await self.groups.current_wars("XYZ"), ([], ["XYZ: clan tag not configured."]))
        await self.record({"state": "notInWar"}, kind="cwl_group", at=NOV)
        self.assertEqual(await self.wars(NOV + 60 * SECOND), [])
        broken = CwlGroups(SimpleNamespace(group=AsyncMock(side_effect=sqlite3.OperationalError("locked"))), CLANS)
        with self.assertLogs("elbow_helper.features.cwl.record", "WARNING"):
            self.assertEqual(await broken.wars("ONE"), [])
        self.assertEqual(await broken.spin_status("ONE", datetime(2026, 11, 2, tzinfo=timezone.utc)), "unavailable")

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


class HealthLeagueTests(LeagueFixture, unittest.IsolatedAsyncioTestCase):
    async def test_a_player_swapped_out_before_battle_day_never_joined_the_war(self):
        from elbow_helper.features.clan_health.record_wars import RegularWars
        rounds = [{"warTags": ["#R0", "#R2"]}]
        await self.record(group(rounds=rounds, season="2026-11"), kind="cwl_group", at=NOV - 2 * DAY)
        await self.record(league_war("preparation", 1), kind="cwl_war", scope="#R0", at=NOV - DAY)
        # Before battle day #P8 takes #P2's place.
        await self.record(swapped(league_war("preparation", 1)), kind="cwl_war", scope="#R0",
                          at=NOV - DAY + 600 * SECOND)
        await self.record(swapped(league_war("warEnded", 1)), kind="cwl_war", scope="#R0",
                          at=end_of(1) + 90 * SECOND)
        league = RegularWars(WarReads(self.store), CLANS, types=("CWL",))
        window = {"cycle_start_ts": (NOV - 2 * DAY) // SECOND, "cycle_end_ts": (end_of(1) + DAY) // SECOND}
        activity = await asyncio.to_thread(league.activity, **window, player_tags={"#P0", "#P2", "#P8"})
        self.assertEqual({tag: (row["wars"], row["attacks_expected"], row["attacks_used"]) for tag, row in activity.items()},
                         {"#P0": (1, 1, 1), "#P8": (1, 1, 1)})
        self.assertEqual(await asyncio.to_thread(league.war_counts, **window), {"ONE": 1})
        (attack,) = await asyncio.to_thread(league.attacks, player_tag="#P8", **window)
        self.assertEqual((attack["war_type"], attack["defender_map_position"], attack["fresh_attack"]), ("CWL", 2, True))

    async def test_a_copied_base_keeps_its_old_position_in_the_attack_sheet(self):
        from elbow_helper.features.clan_health.record_wars import RegularWars
        await self.week(positions=(30, 40), defender_positions=(31, 41))
        await self.run_cwl()
        league = RegularWars(WarReads(self.store), CLANS, types=("CWL",))
        rows = await asyncio.to_thread(league.attacks, player_tag="#P2", cycle_start_ts=0, cycle_end_ts=NOV // SECOND)
        self.assertEqual([(row["defender_map_position"], row["defender_townhall"]) for row in rows], [(41, 17), (41, 17)])


if __name__ == "__main__":
    unittest.main()
