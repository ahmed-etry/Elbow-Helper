"""The CWL copy: closed seasons from the final lineup, partial opponents, checked against the war log, all or nothing."""

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.collectors.wars import due_jobs
from elbow_helper.data.reads.coverage import war_side_coverage
from elbow_helper.data.reads.wars import member_rows
from elbow_helper.data.store import wars_import, wars_import_cwl
from elbow_helper.data.store.legacy_links import ImportMismatch
from elbow_helper.data.store.link_state import receipt
from elbow_helper.data.store.war_projection import api_time
from elbow_helper.data.store.wars import finish_seasons
from elbow_helper.data.store.wars_import import copy_at_startup
from elbow_helper.data.store.wars_import_cwl import CUTOFF, import_cwl_wars
from elbow_helper.data.store.wars_import_plan import snapshot_wars

from .test_wars import END, SECOND, group, war
from .test_wars_import import CLANS, DAY, END_S, ImportFixture, old_id, old_lineup, old_war

WEEK_END = END_S - 20 * 86400
FROZEN = END + 30 * DAY
CUTOFF_S = CUTOFF // SECOND


def league_week(c, *, code="ONE", tag="#PY", prefix="#RR", opponents=("#C0", "#C2", "#C8"), lineup=("#P0", "#P2"),
                season="2026-10", label="2026-10-01", league="Crystal League I", first_end=WEEK_END,
                source="war_cwl_api", positions=(1, 2), defender_positions=(1, 2), state="warEnded", rounds=(1, 2, 3)):
    """A league week of the old store: the first player attacks every round, the second misses round 2,
    and one player was swapped out of round 1 before battle day."""
    ids = []
    for index, round_no in enumerate(rounds):
        war_tag, end = prefix + "0289"[index], first_end + index * 86400
        war_id = "CWL:" + war_tag
        c.execute("INSERT INTO wars(war_id,war_type,clan_code,clan_tag,opponent_tag,opponent_name,cwl_season,"
                  "cwl_season_label,cwl_league,cwl_round,team_size,attacks_per_member,state,preparation_start_ts,"
                  "start_ts,end_ts,last_seen_ts,source) VALUES (?,'CWL',?,?,?,'Rival',?,?,?,?,2,1,?,?,?,?,?,?)",
                  (war_id, code, tag, opponents[index], season, label, league, round_no, state, end - 2 * 86400,
                   end - 86400, end, end + 600, source))
        for order, (player, position) in enumerate(zip(lineup, positions), 1):
            used = int(not (order == 2 and round_no == 2))
            stars = 3 if order == 1 else 2
            c.execute("INSERT INTO war_roster_members(war_id,clan_code,player_tag,player_name,townhall,map_position,"
                      "attacks_expected,attacks_used,roster_state,captured_ts,source) VALUES (?,?,?,'Old',16,?,1,?,"
                      "'warEnded',?,?)", (war_id, code, player, position, used, end + 60, source))
            c.execute("INSERT INTO war_activity(war_id,war_type,clan_code,clan_tag,end_ts,player_tag,player_name,"
                      "attacks_expected,attacks_used,stars,destruction,attack_count,source) "
                      "VALUES (?,'CWL',?,?,?,?,'Old',1,?,?,?,?,?)",
                      (war_id, code, tag, end, player, used, stars * used, 100 * used, used, source))
            if used:
                c.execute("INSERT INTO war_attacks(war_id,war_type,clan_code,clan_tag,end_ts,war_state,player_tag,"
                          "player_name,attack_order,defender_tag,defender_name,defender_map_position,defender_townhall,"
                          "stars,destruction,fresh_attack,duration,source) "
                          "VALUES (?,'CWL',?,?,?,'warEnded',?,'Old',?,?,'Them',?,?,?,?,1,90,?)",
                          (war_id, code, tag, end, player, order, ("#Q0", "#Q2")[order - 1],
                           defender_positions[order - 1], 15 + order, stars, 100.0 if stars == 3 else 70.0, source))
        if index == 0:
            c.execute("INSERT INTO war_activity(war_id,war_type,clan_code,clan_tag,end_ts,player_tag,player_name,"
                      "attacks_expected,attacks_used,stars,destruction,attack_count,source) "
                      "VALUES (?,'CWL',?,?,?,'#P8','Swapped',1,0,0,0,0,?)", (war_id, code, tag, end - 86400, source))
        ids.append(war_id)
    return ids


class CwlFixture(ImportFixture):
    async def run_cwl(self, at=FROZEN):
        return await self.store.call(import_cwl_wars, frozen=snapshot_wars(self.health, None, at), clans=CLANS)

    async def week(self, **kwargs):
        with self.old() as old, old:
            return league_week(old, **kwargs)

    async def log_summary(self, *, tag="#PY", end=WEEK_END + 2 * 86400, attacks=5, stars=23, team=2):
        def write(c):
            item = receipt(c, "test", "test", 0, 0)
            run = c.execute("SELECT run_id FROM collection_items WHERE id=?", (item,)).fetchone()[0]
            c.execute("INSERT INTO cwl_log_summaries(clan_tag,end_at,team_size,attacks_per_member,attacks,stars,"
                      "destruction,source_run_id) VALUES (?,?,?,1,?,?,250.0,?)",
                      (tag, end * SECOND, team, attacks, stars, run))
            c.commit()
        await self.store.call(write)

    async def war_of(self, war_tag):
        return (await self.rows("SELECT * FROM wars WHERE war_tag=?", (war_tag,)))[0]

    async def misses(self, war_tag, clan):
        def read(c):
            row = c.execute("SELECT * FROM wars WHERE war_tag=?", (war_tag,)).fetchone()
            return {r["player_tag"]: r["missed"] for r in member_rows(c, row, clan, war_side_coverage(c, row["id"], clan))}
        return await self.store.call(read)


class CopyTests(CwlFixture, unittest.IsolatedAsyncioTestCase):
    async def test_a_week_becomes_a_closed_season_with_the_family_side_and_partial_opponents(self):
        await self.execute("INSERT INTO accounts(player_tag,current_name,first_observed_at,tracking_status) "
                           "VALUES ('#P0','Old',0,'tracked')")
        old_ids = await self.week()
        result = await self.run_cwl()
        self.assertTrue(result["imported"])
        checks = result["checks"]
        self.assertEqual(checks["outcomes"], {
            "war_activity": {"excluded:swapped_out": 1, "matched": 6}, "war_attacks": {"imported": 5},
            "war_roster_members": {"imported": 6}, "wars": {"imported": 3}})
        self.assertEqual(checks["inserted"], {"seasons": 1, "wars": 3, "lineup_rows": 6, "opponent_members": 5,
                                              "attacks": 5})
        season = (await self.rows("SELECT * FROM cwl_seasons"))[0]
        self.assertEqual((season["season_key"], season["group_key"], season["state"], season["final_at"],
                          season["league_name"], season["league_id"]),
                         ("2026-10", "#C0:#C2:#C8:#PY", "ended", FROZEN, "Crystal League I", None))
        legacy = json.loads(season["group_metadata_json"])["legacy"]
        self.assertEqual((legacy["season_key"], legacy["season_label"], legacy["league"]),
                         ("2026-10", "2026-10-01", "Crystal League I"))
        self.assertEqual(await self.rows("SELECT clan_tag FROM cwl_season_sources"), [{"clan_tag": "#PY"}])
        self.assertEqual([r["clan_tag"] for r in await self.rows("SELECT clan_tag FROM cwl_clans ORDER BY clan_tag")],
                         ["#C0", "#C2", "#C8", "#PY"])
        players = await self.rows("SELECT player_tag,account_id IS NOT NULL AS linked,"
                                  "json_extract(extra_json,'$.legacy.wars') AS wars FROM cwl_players ORDER BY player_tag")
        # The swapped-out player never joins the season's players.
        self.assertEqual([tuple(p.values()) for p in players], [("#P0", 1, 3), ("#P2", 0, 3)])
        rounds = await self.rows("SELECT r.round_no,r.war_tag,w.type FROM cwl_rounds r JOIN wars w ON w.id=r.war_id "
                                 "ORDER BY r.round_no")
        self.assertEqual([tuple(r.values()) for r in rounds], [(1, "#RR0", "CWL"), (2, "#RR2", "CWL"), (3, "#RR8", "CWL")])
        first = await self.war_of("#RR0")
        self.assertEqual((first["type_basis"], first["state"], first["end_at"], first["team_size"],
                          first["roster_complete"], first["attacks_complete"], first["result_complete"]),
                         ("war_tag", "warEnded", WEEK_END * SECOND, 2, 1, 1, 0))
        sides = {r["clan_tag"]: r for r in await self.rows("SELECT * FROM war_sides WHERE war_id=?", (first["id"],))}
        # Best attack per base: 3 + 2 stars, (100 + 70) / 2 destruction; marked as the copy's own sums.
        own = sides["#PY"]
        self.assertEqual((own["stars"], own["destruction"], own["attacks"], own["result"]), (5, 85.0, 2, None))
        self.assertEqual(json.loads(own["extra_json"])["derived"], ["attacks", "destruction", "stars"])
        other = sides["#C0"]
        self.assertEqual((other["name"], other["stars"], other["destruction"], other["attacks"]), ("Rival", None, None, None))
        self.assertTrue(json.loads(other["extra_json"])["partial"])
        defenders = await self.rows("SELECT player_tag,townhall,source_map_position,attacks_expected,attacks_used,"
                                    "json_extract(extra_json,'$.partial') AS partial FROM war_members "
                                    "WHERE war_id=? AND side=? ORDER BY player_tag", (first["id"], other["side"]))
        self.assertEqual([tuple(d.values()) for d in defenders], [("#Q0", 16, 1, None, None, 1), ("#Q2", 17, 2, None, None, 1)])
        attacks = await self.rows("SELECT a.attack_order,m.player_tag AS attacker,d.player_tag AS defender,"
                                  "json_extract(a.extra_json,'$.legacy.fresh_attack') AS fresh FROM war_attacks a "
                                  "JOIN war_members m ON m.id=a.attacker_member_id JOIN war_members d "
                                  "ON d.id=a.defender_member_id WHERE a.war_id=? ORDER BY a.attack_order", (first["id"],))
        self.assertEqual([tuple(a.values()) for a in attacks], [(1, "#P0", "#Q0", 1), (2, "#P2", "#Q2", 1)])
        aliases = await self.rows("SELECT legacy_id,clan_code,legacy_label,capture_state FROM war_legacy_ids ORDER BY legacy_id")
        self.assertEqual([a["legacy_id"] for a in aliases], sorted(old_ids))
        self.assertEqual({(a["clan_code"], a["legacy_label"], a["capture_state"]) for a in aliases}, {("ONE", "CWL", "warEnded")})
        self.assertEqual(await self.rows("SELECT scope_id,enabled FROM war_collection_state WHERE source_kind='cwl_war' "
                                         "ORDER BY scope_id"),
                         [{"scope_id": tag, "enabled": 0} for tag in ("#RR0", "#RR2", "#RR8")])
        # Misses come from the final lineup only; nothing is ever read as a miss on the opponent's side.
        self.assertEqual(await self.misses("#RR2", "#PY"), {"#P0": 0, "#P2": 1})
        self.assertEqual(await self.misses("#RR0", "#PY"), {"#P0": 0, "#P2": 0})
        self.assertEqual(await self.misses("#RR0", "#C0"), {"#Q0": None, "#Q2": None})
        week = checks["seasons"][0]
        self.assertEqual((week["clan"], week["wars"], week["roster_rows"], week["attacks"], week["opponent_members"],
                          week["unlinked_accounts"], week["position_basis"], week["log_check"]["status"]),
                         ("ONE", 3, 6, 5, 5, 3, {"lineup": 3}, "no_summary"))
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])

    async def test_season_keys_labels_and_old_positions_are_kept_as_they_were(self):
        # The month's second league keeps its -catchup key; the oldest weeks kept league-roster positions.
        await self.week(season="2026-06-catchup", label="2026-06-catchup", league="", source="legacy_cwl_bonus_cache",
                        positions=(7, 12), defender_positions=(3, 20))
        await self.week(prefix="#RG", opponents=("#J0", "#J2", "#J8"), season="2026-06", label="2026-06-01",
                        first_end=WEEK_END - 20 * 86400)
        await self.run_cwl()
        seasons = {row["season_key"]: row for row in await self.rows("SELECT * FROM cwl_seasons")}
        self.assertEqual(set(seasons), {"2026-06-catchup", "2026-06"})
        self.assertIsNone(seasons["2026-06-catchup"]["league_name"])
        self.assertEqual(json.loads(seasons["2026-06"]["group_metadata_json"])["legacy"]["season_label"], "2026-06-01")
        legacy, current = await self.war_of("#RR0"), await self.war_of("#RG0")
        positions = await self.rows("SELECT player_tag,source_map_position FROM war_members WHERE war_id=? ORDER BY player_tag",
                                    (legacy["id"],))
        self.assertEqual([tuple(p.values()) for p in positions], [("#P0", 7), ("#P2", 12), ("#Q0", 3), ("#Q2", 20)])
        bases = {}
        for war_row in (legacy, current):
            for side in await self.rows("SELECT clan_tag,extra_json FROM war_sides WHERE war_id=?", (war_row["id"],)):
                bases[war_row["war_tag"], side["clan_tag"]] = json.loads(side["extra_json"])["legacy"]["position_basis"]
        self.assertEqual(bases, {("#RR0", "#PY"): "league_roster", ("#RR0", "#C0"): "league_roster",
                                 ("#RG0", "#PY"): "lineup", ("#RG0", "#J0"): "lineup"})
        member = (await self.rows("SELECT extra_json FROM war_members WHERE war_id=? AND player_tag='#P0'",
                                  (legacy["id"],)))[0]
        self.assertEqual(json.loads(member["extra_json"])["legacy"]["position_basis"], "league_roster")

    async def test_weeks_the_collector_owns_or_that_are_not_whole_stay_out_with_their_reason(self):
        await self.week()
        # November's week belongs to the collector, even once it has ended.
        await self.week(prefix="#RG", opponents=("#J0", "#J2", "#J8"), season="2026-11", label="2026-11-01",
                        first_end=CUTOFF_S + 2 * 86400)
        # A week with a round missing, and a week whose last round had not ended.
        await self.week(code="TWO", tag="#LQ", prefix="#RL", lineup=("#L0", "#L2"), season="2026-09", rounds=(1, 3, 4))
        await self.week(code="TWO", tag="#LQ", prefix="#RY", lineup=("#L0", "#L2"), season="2026-08",
                        first_end=WEEK_END - 60 * 86400, state="inWar")
        with self.old() as old, old:
            old.execute("INSERT INTO war_roster_members(war_id,clan_code,player_tag,player_name,townhall,map_position,"
                        "attacks_expected,attacks_used,roster_state,captured_ts,source) "
                        "VALUES ('CWL:#UU','ONE','#P0','Old',16,1,1,1,'warEnded',1,'x')")
        result = await self.run_cwl()
        checks = result["checks"]
        self.assertEqual(sorted((item["clan"], item["season_key"], item["reason"]) for item in checks["left_out"]), [
            ("ONE", "2026-11", "collector_season"), ("TWO", "2026-08", "not_ended"), ("TWO", "2026-09", "incomplete_group")])
        self.assertEqual(checks["outcomes"]["wars"], {"excluded:collector_season": 3, "excluded:incomplete_group": 3,
                                                      "excluded:not_ended": 3, "imported": 3})
        self.assertEqual(checks["outcomes"]["war_roster_members"]["excluded:no_war_row"], 1)
        self.assertEqual(checks["unplaced"], {"ONE": {"war_roster_members": {"no_war_row": 1}}})
        self.assertEqual([r["season_key"] for r in await self.rows("SELECT season_key FROM cwl_seasons")], ["2026-10"])
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 3)
        # A week that ends after the copy reads the old files is not copied either.
        await self.execute("DELETE FROM imports")
        before = await self.dump()
        early = await self.run_cwl(at=(WEEK_END + 86400) * SECOND)
        self.assertIn(("ONE", "2026-10", "not_ended"),
                      [(item["clan"], item["season_key"], item["reason"]) for item in early["checks"]["left_out"]])
        self.assertEqual([line for line in await self.dump() if "cwl_seasons" in line],
                         [line for line in before if "cwl_seasons" in line])

    async def test_the_war_log_checks_each_week_and_a_mismatch_keeps_nothing(self):
        await self.week()
        # 13 stars from the attacks plus two war wins.
        await self.log_summary(stars=33)
        result = await self.run_cwl()
        check = result["checks"]["seasons"][0]["log_check"]
        self.assertEqual((check["status"], check["attacks"], check["team_size"], check["stars"], check["win_bonus"]),
                         ("matched", [5, 5], [[2], 2], [13, 33], 20))
        self.assertEqual(result["checks"]["log_checks"], {"matched": 1})
        for attacks, stars, team in ((6, 33, 2), (5, 34, 2), (5, 3, 2), (5, 63, 2), (5, 33, 3)):
            with self.subTest(attacks=attacks, stars=stars, team=team):
                await self.execute("DELETE FROM cwl_log_summaries")
                await self.execute("DELETE FROM imports")
                for table in ("war_collection_state", "war_legacy_ids", "war_attacks", "war_members", "war_source_keys",
                              "war_sides", "cwl_rounds", "cwl_players", "cwl_clans", "cwl_season_sources",
                              "cwl_seasons", "wars"):
                    await self.execute(f"DELETE FROM {table}")
                await self.log_summary(attacks=attacks, stars=stars, team=team)
                before = await self.dump()
                with self.assertRaisesRegex(ImportMismatch, "cwl_log_mismatch") as caught:
                    await self.run_cwl()
                self.assertEqual(caught.exception.checks["log_checks"], {"mismatch": 1})
                self.assertEqual(await self.dump(), before)

    async def test_rows_that_do_not_add_up_stop_the_copy_and_keep_nothing(self):
        cases = (
            ("UPDATE war_attacks SET player_tag='#P8' WHERE attack_order=2 AND war_id='CWL:#RR0'",
             "cwl_attacker_without_roster"),
            ("DELETE FROM war_attacks WHERE attack_order=2 AND war_id='CWL:#RR0'", "cwl_attack_count_mismatch"),
            ("UPDATE war_attacks SET defender_tag='#Q0' WHERE attack_order=2 AND war_id='CWL:#RR0'",
             "cwl_defender_conflict"),
            ("UPDATE war_attacks SET defender_tag='#P2' WHERE attack_order=1 AND war_id='CWL:#RR0'",
             "cwl_defender_on_own_side"),
            ("UPDATE war_attacks SET attack_order=1 WHERE attack_order=2 AND war_id='CWL:#RR0'",
             "cwl_attack_order_repeated"),
            ("UPDATE war_activity SET attacks_used=1 WHERE player_tag='#P8'", "cwl_activity_outside_roster"),
            ("DELETE FROM war_roster_members WHERE war_id='CWL:#RR8'; DELETE FROM war_attacks WHERE war_id='CWL:#RR8'",
             "cwl_lineup_missing"),
            ("UPDATE wars SET opponent_tag='#LQ' WHERE war_id='CWL:#RR0'", "cwl_family_opponent"),
            ("UPDATE wars SET cwl_season='October' WHERE war_id='CWL:#RR0'", "bad_row:wars"),
            ("UPDATE wars SET start_ts=end_ts+1 WHERE war_id='CWL:#RR0'", "bad_row:wars"),
            ("UPDATE war_attacks SET stars=4", "bad_row:war_attacks"),
            ("UPDATE war_roster_members SET player_tag='not a tag' WHERE player_tag='#P0'", "bad_row:war_roster_members"),
            ("UPDATE war_activity SET attacks_used=-1 WHERE player_tag='#P8'", "bad_row:war_activity"))
        before = await self.dump()
        for statements, reason in cases:
            with self.subTest(reason=reason, statements=statements):
                with self.old() as old, old:
                    old.executescript("DELETE FROM wars; DELETE FROM war_roster_members; DELETE FROM war_attacks; "
                                      "DELETE FROM war_activity;")
                    league_week(old)
                    old.executescript(statements)
                with self.assertRaisesRegex(ImportMismatch, reason):
                    await self.run_cwl()
                self.assertEqual(await self.dump(), before)
        # One war tag under two clans is not a league week the copy can place.
        with self.old() as old, old:
            old.executescript("DELETE FROM wars; DELETE FROM war_roster_members; DELETE FROM war_attacks; "
                              "DELETE FROM war_activity;")
            league_week(old)
            league_week(old, code="TWO", tag="#LQ", lineup=("#L0", "#L2"), first_end=WEEK_END - 40 * 86400)
        with self.assertRaisesRegex(ImportMismatch, "cwl_war_tag_shared"):
            await self.run_cwl()
        self.assertEqual(await self.rows("SELECT * FROM imports"), [])

    async def test_the_records_own_league_wars_are_kept_and_a_different_one_stops_the_copy(self):
        # The collector holds one round of the week, with the same facts as the old store.
        await self.record(war("warEnded", clans=("#PY", "#GR")), kind="cwl_war", scope="#RR0", at=END + 90 * SECOND)
        week_end = END_S
        with self.old() as old, old:
            league_week(old, opponents=("#GR", "#C2", "#C8"), first_end=week_end)
            old.execute("UPDATE wars SET attacks_per_member=2 WHERE war_id='CWL:#RR0'")
        before = await self.dump()
        with self.assertRaisesRegex(ImportMismatch, "cwl_group_partly_in_record"):
            await self.run_cwl()
        self.assertEqual(await self.dump(), before)
        # A war tag the record holds with other facts.
        with self.old() as old, old:
            old.execute("UPDATE wars SET end_ts=end_ts+3600 WHERE war_id='CWL:#RR0'")
        with self.assertRaisesRegex(ImportMismatch, "cwl_record_conflict"):
            await self.run_cwl()
        self.assertEqual(await self.dump(), before)

    async def test_a_repeat_adds_nothing_even_without_its_marker(self):
        await self.week()
        await self.run_cwl()
        settled = await self.dump()
        again = await self.run_cwl()
        self.assertFalse(again["imported"])
        self.assertEqual(await self.dump(), settled)
        await self.execute("DELETE FROM imports WHERE topic='wars_cwl'")
        rerun = await self.run_cwl()
        self.assertTrue(rerun["imported"])
        self.assertEqual(rerun["checks"]["inserted"], {"seasons": 0, "wars": 0, "lineup_rows": 0, "opponent_members": 0,
                                                       "attacks": 0})
        self.assertEqual(rerun["checks"]["outcomes"]["wars"], {"kept_record": 3})
        self.assertTrue(rerun["checks"]["seasons"][0]["kept_record"])
        for table in ("wars", "war_sides", "war_members", "war_attacks", "war_legacy_ids", "war_source_keys",
                      "cwl_seasons", "cwl_clans", "cwl_players", "cwl_rounds", "war_collection_state"):
            self.assertEqual([line for line in settled if f'INSERT INTO "{table}"' in line],
                             [line for line in await self.dump() if f'INSERT INTO "{table}"' in line], table)

    async def test_copied_seasons_and_their_wars_are_never_polled(self):
        await self.week()
        await self.run_cwl()
        clans = [SimpleNamespace(tag=tag) for tag in CLANS.values()]
        late = int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp()) * SECOND
        jobs = await self.store.call(due_jobs, clans, late)
        self.assertEqual({job["source_kind"] for job in jobs}, {"current_war", "war_log"})
        early = int(datetime(2026, 12, 2, tzinfo=timezone.utc).timestamp()) * SECOND
        self.assertNotIn("cwl_war", {job["source_kind"] for job in await self.store.call(due_jobs, clans, early)})
        final = (await self.rows("SELECT final_at FROM cwl_seasons"))[0]["final_at"]
        await self.store.call(lambda c: finish_seasons(c, late))
        self.assertEqual((await self.rows("SELECT final_at FROM cwl_seasons"))[0]["final_at"], final)


class StartupTests(CwlFixture, unittest.IsolatedAsyncioTestCase):
    """Both parts are copied once, at startup, in one transaction."""

    def collectors(self):
        bot = SimpleNamespace(boot_complete=asyncio.Event(), guild_id=1, get_guild=lambda guild_id: None)
        return FamilyCollectors(bot, self.store, SimpleNamespace(get=AsyncMock()), (), old_wars=(self.health, None))

    def startup(self):
        def copy(connection, health, cache):
            return copy_at_startup(connection, health, cache, clans=CLANS)
        return patch("elbow_helper.data.collectors.service.copy_at_startup", copy)

    async def regular_and_league(self):
        with self.old() as old, old:
            old_war(old, old_id(END_S))
            old_lineup(old, old_id(END_S), [("#P0", 1), ("#P2", 1)])
            league_week(old)

    async def test_one_start_copies_both_parts_and_a_later_start_reads_nothing(self):
        await self.regular_and_league()
        with self.startup(), self.assertLogs("elbow_helper.data.collectors.service", level="INFO") as logs:
            await self.collectors()._copy_old_wars()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("1 league weeks (1 found), 3 league wars, 6 league lineup rows, 5 league attacks", logs.output[0])
        self.assertEqual({r["topic"] for r in await self.rows("SELECT topic FROM imports")}, {"wars_regular", "wars_cwl"})
        settled = await self.dump()
        with self.startup(), patch.object(wars_import, "snapshot_wars", side_effect=AssertionError("read")), \
                self.assertNoLogs("elbow_helper.data.collectors.service", level="INFO"):
            await self.collectors()._copy_old_wars()
        self.assertEqual(await self.dump(), settled)

    async def test_a_record_with_its_regular_wars_copies_only_the_league_wars(self):
        await self.regular_and_league()
        await self.run_import()
        regular = await self.rows("SELECT * FROM imports WHERE topic='wars_regular'")
        result = await self.store.call(copy_at_startup, self.health, None, clans=CLANS)
        self.assertIsNone(result["regular"])
        self.assertTrue(result["cwl"]["imported"])
        self.assertEqual(await self.rows("SELECT * FROM imports WHERE topic='wars_regular'"), regular)
        self.assertIsNone(await self.store.call(copy_at_startup, self.health, None, clans=CLANS))

    async def test_a_failed_league_copy_keeps_neither_part_and_startup_goes_on(self):
        await self.regular_and_league()
        before = await self.dump()
        with self.startup(), patch.object(wars_import_cwl.CwlImporter, "verify",
                                          side_effect=ImportMismatch("forced_check_failure")), \
                self.assertLogs("elbow_helper.data.collectors.service", level="INFO") as logs:
            await self.collectors()._copy_old_wars()
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])
        self.assertIn("ImportMismatch: forced_check_failure", logs.output[0])
        self.assertEqual(await self.dump(), before)
        # Bad league rows stop the regular part too; the next start tries both again.
        with self.old() as old, old:
            old.execute("UPDATE war_attacks SET stars=4 WHERE war_type='CWL'")
        with self.assertRaisesRegex(ImportMismatch, "bad_row:war_attacks"):
            await self.store.call(copy_at_startup, self.health, None, clans=CLANS)
        self.assertEqual(await self.dump(), before)
        with self.old() as old, old:
            old.execute("UPDATE war_attacks SET stars=3 WHERE war_type='CWL'")
        with self.startup():
            await self.collectors()._copy_old_wars()
        self.assertEqual({r["topic"] for r in await self.rows("SELECT topic FROM imports")}, {"wars_regular", "wars_cwl"})


NOVEMBER = (("20261102T080000.000Z", "20261103T080000.000Z", "20261104T080000.000Z"),
            ("20261103T080000.000Z", "20261104T080000.000Z", "20261105T080000.000Z"))
NOVEMBER_AT = api_time("20261104T120000.000Z")


def november_war(state, clans, round_index):
    payload = war(state, clans=clans, end=NOVEMBER[round_index][2])
    payload["preparationStartTime"], payload["startTime"] = NOVEMBER[round_index][:2]
    return payload


class LivePathTests(CwlFixture, unittest.IsolatedAsyncioTestCase):
    """The live bot: regular wars already copied, November saved by the collector, the old bot still writing."""

    async def collector_november(self, round_one=("#V0", "#VP")):
        """November as the collector saves it: round 1 ended, round 2 in battle day, round 3 not drawn yet."""
        rounds = [{"warTags": list(round_one)}, {"warTags": ["#V2", "#VL"]}, {"warTags": ["#0", "#0"]}]
        await self.record(group(rounds=rounds, season="2026-11"), kind="cwl_group", scope="#PY", at=NOVEMBER_AT)
        for tag, clans, round_index, state in ((round_one[0], ("#PY", "#GR"), 0, "warEnded"),
                                               (round_one[1], ("#LQ", "#CU"), 0, "warEnded"),
                                               ("#V2", ("#PY", "#CU"), 1, "inWar"), ("#VL", ("#LQ", "#GR"), 1, "inWar")):
            await self.record(november_war(state, clans, round_index), kind="cwl_war", scope=tag, at=NOVEMBER_AT)

    async def regular_copied(self, *, november=True):
        with self.old() as old, old:
            old_war(old, old_id(END_S))
            old_lineup(old, old_id(END_S), [("#P0", 1), ("#P2", 1)])
            league_week(old)
            if november:
                # The old bot writes the running week too, under the collector's war tags.
                league_week(old, prefix="#V", opponents=("#GR", "#CU", "#C8"), season="2026-11", label="2026-11",
                            first_end=api_time(NOVEMBER[0][2]) // SECOND, rounds=(1, 2))
                old.execute("UPDATE wars SET state='inWar' WHERE war_id='CWL:#V2'")
        await self.run_import()

    def start(self):
        """A start of this code, frozen at a fixed time, with the regular part failing if it ever ran."""
        frozen = patch.object(wars_import, "snapshot_wars", lambda health, cache: snapshot_wars(health, cache, FROZEN))
        regular = patch.object(wars_import, "write_regular", side_effect=AssertionError("regular part ran"))
        return frozen, regular

    async def test_only_the_league_part_runs_and_november_stays_with_the_collector(self):
        await self.regular_copied()
        await self.collector_november()
        november_wars = await self.rows("SELECT war_tag,state FROM wars WHERE war_tag LIKE '#V%' ORDER BY war_tag")
        self.assertEqual([tuple(w.values()) for w in november_wars], [
            ("#V0", "warEnded"), ("#V2", "inWar"), ("#VL", "inWar"), ("#VP", "warEnded")])
        before = await self.dump()
        frozen, regular = self.start()
        with frozen, regular:
            result = await self.store.call(copy_at_startup, self.health, None, clans=CLANS)
        self.assertIsNone(result["regular"])
        checks = result["cwl"]["checks"]
        self.assertEqual(checks["left_out"], [{"clan": "ONE", "season_key": "2026-11", "reason": "collector_season",
                                               "wars": 2}])
        self.assertEqual(checks["outcomes"]["wars"], {"excluded:collector_season": 2, "imported": 3})
        self.assertEqual(checks["inserted"]["seasons"], 1)
        # Nothing already in the record changes: the regular wars, their marker, and all of November.
        after = await self.dump()
        self.assertEqual(sorted(set(before) - set(after)), [])
        november = (await self.rows("SELECT * FROM cwl_seasons WHERE season_key='2026-11'"))[0]
        self.assertEqual((november["state"], november["final_at"]), ("inWar", None))
        self.assertEqual((await self.rows("SELECT final_at FROM cwl_seasons WHERE season_key='2026-10'"))[0]["final_at"],
                         FROZEN)
        clans = [SimpleNamespace(tag=tag) for tag in CLANS.values()]
        soon = await self.store.call(due_jobs, clans, NOVEMBER_AT + 3600 * SECOND)
        self.assertEqual({j["scope_id"] for j in soon if j["source_kind"] == "cwl_war"}, {"#V2", "#VL"})
        late = await self.store.call(due_jobs, clans, int(datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp()) * SECOND)
        # Outside league days only the open November season keeps its clans' groups polled.
        self.assertEqual({j["scope_id"] for j in late if j["source_kind"] == "cwl_group"}, {"#PY", "#LQ"})
        self.assertEqual({j["scope_id"] for j in late if j["source_kind"] == "cwl_war"}, {"#V2", "#VL"})
        frozen, regular = self.start()
        with frozen, regular:
            self.assertIsNone(await self.store.call(copy_at_startup, self.health, None, clans=CLANS))

    async def test_a_november_round_under_an_old_war_tag_stops_the_copy_and_changes_neither_side(self):
        await self.regular_copied(november=False)
        # The collector holds a November war under the tag the old store has for October's round 1.
        await self.collector_november(round_one=("#RR0", "#VP"))
        saved = await self.rows("SELECT * FROM wars WHERE war_tag='#RR0'")
        before, old_file = await self.dump(), self.health.read_bytes()
        frozen, regular = self.start()
        with frozen, regular, self.assertRaisesRegex(ImportMismatch, "cwl_record_conflict"):
            await self.store.call(copy_at_startup, self.health, None, clans=CLANS)
        self.assertEqual(await self.dump(), before)
        self.assertEqual(self.health.read_bytes(), old_file)
        self.assertEqual(await self.rows("SELECT * FROM wars WHERE war_tag='#RR0'"), saved)
        self.assertEqual(saved[0]["end_at"], api_time(NOVEMBER[0][2]))
        # The bot still starts, and the next start refuses the same way.
        bot = SimpleNamespace(boot_complete=asyncio.Event(), guild_id=1, get_guild=lambda guild_id: None)
        collectors = FamilyCollectors(bot, self.store, SimpleNamespace(get=AsyncMock()), (), old_wars=(self.health, None))
        copy = patch("elbow_helper.data.collectors.service.copy_at_startup",
                     lambda connection, health, cache: copy_at_startup(connection, health, cache, clans=CLANS))
        frozen, regular = self.start()
        with frozen, regular, copy, self.assertLogs("elbow_helper.data.collectors.service", level="INFO") as logs:
            await collectors._copy_old_wars()
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])
        self.assertIn("cwl_record_conflict", logs.output[0])
        self.assertEqual(await self.dump(), before)


if __name__ == "__main__":
    unittest.main()
