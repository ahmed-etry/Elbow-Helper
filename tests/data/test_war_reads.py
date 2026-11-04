"""Clan-facing war reads over synthetic record wars, including partial and private paths."""

from contextlib import closing
import sqlite3
import unittest

from elbow_helper.data.reads import WarReads
from elbow_helper.data.store.war_projection import HOUR, api_time
from elbow_helper.features.clan_health.database.schema import create_schema

from . import test_family_store as fixtures
from .test_family_store import link
from .test_wars import END, SECOND, START, WarFixture, war
from .war_parity import compare_frozen_wars


DAY = 24 * HOUR
BADGE = {"small": "https://example.test/badge"}
OCTOBER, NOVEMBER = api_time("20261001T000000.000Z"), api_time("20261101T000000.000Z")


def friendly(end="20261030T180000.000Z", *, clans=("#PY", "#CU")):
    # One hour of preparation makes the times decide it was friendly.
    payload = war("warEnded", clans=clans, end=end)
    payload.update(preparationStartTime="20261029T170000.000Z", startTime="20261029T180000.000Z")
    return payload


def log_entry(opponent, end, result):
    return {"endTime": end, "teamSize": 2, "attacksPerMember": 2, "result": result,
            "clan": {"tag": "#PY", "name": "Clan #PY", "clanLevel": 20, "badgeUrls": BADGE,
                     "attacks": 2, "stars": 3, "destructionPercentage": 50.0, "expEarned": 10},
            "opponent": {"tag": opponent, "name": "Other", "clanLevel": 20, "badgeUrls": BADGE,
                         "attacks": 2, "stars": 2, "destructionPercentage": 40.0}}


def league_week(end):
    return {"endTime": end, "teamSize": 15, "attacksPerMember": 1, "result": "win",
            "clan": {"tag": "#PY", "name": "Clan #PY", "clanLevel": 20, "badgeUrls": BADGE,
                     "attacks": 100, "stars": 250, "destructionPercentage": 650.0, "expEarned": 0},
            "opponent": {"badgeUrls": BADGE, "clanLevel": 0, "stars": 0, "destructionPercentage": 0}}


def page(*items):
    return {"items": list(items), "paging": {"cursors": {}}}


class WarReadTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    links = fixtures.FamilyStoreTests.links

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reads = WarReads(self.store)

    async def test_current_and_previous_face_the_requested_clan_like_the_board(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        later = war("inWar", clans=("#PY", "#CU"), end="20261030T180000.000Z")
        later.update(preparationStartTime="20261028T180000.000Z", startTime="20261029T180000.000Z")
        at = END + DAY + HOUR
        await self.record(later, at=at)
        current = await self.reads.current("#PY", at)
        board = current["war"]
        self.assertEqual((current["state"], current["observed_at"], board["clan"]["clan_tag"],
                          board["opponent"]["clan_tag"], board["type"]), ("inWar", at, "#PY", "#CU", "REG"))
        self.assertEqual((board["team_size"], board["clan"]["stars"], board["clan"]["attacks_used"],
                          board["clan"]["attacks_left"], board["opponent"]["attacks_left"]), (2, 3, 1, 3, 3))
        self.assertEqual(board["clan"]["townhall_counts"], [{"townhall": 16, "count": 2}])
        self.assertTrue(current["coverage"]["complete"])
        previous = (await self.reads.previous("#PY", at))["war"]
        self.assertEqual((previous["opponent"]["clan_tag"], previous["state"], previous["result"],
                          previous["result_basis"]), ("#GR", "warEnded", "tie", "final_score"))
        # notInWar has no current war; the last ended war is still the previous one.
        await self.record({"state": "notInWar"}, at=at + 3 * DAY)
        idle = await self.reads.current("#PY", at + 3 * DAY)
        self.assertEqual((idle["state"], idle["war"]), ("notInWar", None))
        self.assertEqual((await self.reads.previous("#PY", at + 3 * DAY))["war"]["opponent"]["clan_tag"], "#CU")
        # A friendly war is the board's previous war unless only regular wars are asked for.
        await self.record(friendly(end="20261103T180000.000Z", clans=("#PY", "#LQ")) | {
            "preparationStartTime": "20261102T170000.000Z", "startTime": "20261102T180000.000Z"}, at=at + 6 * DAY)
        await self.record({"state": "notInWar"}, at=at + 7 * DAY)
        self.assertEqual((await self.reads.previous("#PY", at + 7 * DAY))["war"]["type"], "FRIENDLY")
        self.assertEqual((await self.reads.previous("#PY", at + 7 * DAY, types=["REG"]))["war"]["opponent"]["clan_tag"], "#CU")
        untracked = await self.reads.current("#LQ", at)
        self.assertEqual((untracked["state"], untracked["war"], untracked["coverage"]["signals"][0]["status"]),
                         (None, None, "not_tracked"))
        self.assertIsNone((await self.reads.previous("#LQ", at))["war"])

    async def test_lineups_list_both_sides_with_their_attacks_only_when_asked(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        later = war("inWar", clans=("#PY", "#CU"), end="20261030T180000.000Z")
        later.update(preparationStartTime="20261028T180000.000Z", startTime="20261029T180000.000Z")
        at = END + DAY + HOUR
        await self.record(later, at=at)
        self.assertNotIn("members", (await self.reads.current("#PY", at))["war"]["clan"])
        board = (await self.reads.current("#PY", at, lineups=True))["war"]
        self.assertEqual([(m["player_tag"], m["map_position"], m["townhall"], m["attacks_used"], m["attacks_expected"],
                           [(a["attack_order"], a["defender_tag"], a["stars"], a["destruction"]) for a in m["attacks"]])
                          for m in board["clan"]["members"]],
                         [("#P0", 1, 16, 1, 2, [(1, "#Q0", 3, 100.0)]), ("#P2", 2, 16, 0, 2, [])])
        self.assertEqual([(m["player_tag"], [a["attack_order"] for a in m["attacks"]]) for m in board["opponent"]["members"]],
                         [("#Q0", [2]), ("#Q2", [])])
        previous = (await self.reads.previous("#PY", at, lineups=True))["war"]
        self.assertEqual([len(m["attacks"]) for m in previous["clan"]["members"]], [1, 1])
        self.assertEqual(previous["opponent"]["members"][1]["attacks"][0]["defender_tag"], "#P2")
        # An attack whose attacker is unknown is left out; coverage says the attacks are incomplete.
        await self.execute("UPDATE war_attacks SET attacker_member_id=NULL WHERE war_id=? AND attack_order=1",
                           (board["war_id"],))
        unplaced = (await self.reads.current("#PY", at, lineups=True))["war"]
        self.assertEqual(unplaced["clan"]["members"][0]["attacks"], [])
        self.assertFalse(unplaced["coverage"]["own"]["attacks_complete"])

    async def test_completed_names_misses_keeps_friendly_apart_and_bounds_dates(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.record(friendly(), at=END + 3 * DAY)
        at = END + 4 * DAY
        result = await self.reads.completed(["#PY"], END, END + DAY, as_of=at)
        (row,) = result["rows"]
        self.assertEqual((row["clan"]["clan_tag"], row["opponent"]["clan_tag"], row["result"], row["type"]),
                         ("#PY", "#GR", "tie", "REG"))
        self.assertEqual([(m["player_tag"], m["attacks_used"], m["attacks_expected"], m["missed"])
                          for m in row["missed_attacks"]], [("#P0", 1, 2, 1), ("#P2", 1, 2, 1)])
        # The same war faces each requested clan in its own row.
        facing = await self.reads.completed(["#PY", "#GR"], END, END + DAY, as_of=at)
        self.assertEqual([(r["war_id"], r["clan"]["clan_tag"]) for r in facing["rows"]],
                         [(row["war_id"], "#GR"), (row["war_id"], "#PY")])
        # Exclusive end, inclusive start, and nothing that ended after as_of.
        self.assertEqual((await self.reads.completed(["#PY"], START, END, as_of=at))["rows"], [])
        self.assertEqual((await self.reads.completed(["#PY"], END, END + DAY, as_of=END - 1))["rows"], [])
        window = (END, END + 3 * DAY)
        self.assertEqual([r["type"] for r in (await self.reads.completed(["#PY"], *window, as_of=at))["rows"]], ["REG"])
        both = await self.reads.completed(["#PY"], *window, types=["REG", "FRIENDLY"], as_of=at)
        self.assertEqual([(r["type"], r["type_basis"]) for r in both["rows"]], [("FRIENDLY", "times"), ("REG", "times")])
        page1 = await self.reads.completed(["#PY"], *window, types=["REG", "FRIENDLY"], as_of=at, limit=1)
        page2 = await self.reads.completed(["#PY"], *window, types=["REG", "FRIENDLY"], as_of=at, limit=1,
                                           offset=page1["next_offset"])
        self.assertEqual([page1["rows"][0]["war_id"], page2["rows"][0]["war_id"], page2["next_offset"]],
                         [both["rows"][0]["war_id"], both["rows"][1]["war_id"], None])
        self.assertTrue(page1["coverage"]["truncated"])

    async def test_incomplete_lineups_and_unfinished_wars_leave_misses_unknown(self):
        partial = war("warEnded")
        partial["clan"].pop("members")
        await self.record(partial, at=END + 90 * SECOND)
        # The last capture was mid-war; the end passed without a final check.
        unfinished = war("inWar", clans=("#PY", "#CU"), end="20261030T180000.000Z")
        unfinished.update(preparationStartTime="20261028T180000.000Z", startTime="20261029T180000.000Z")
        await self.record(unfinished, at=END + DAY + HOUR)
        at = END + 5 * DAY
        result = await self.reads.completed(["#PY"], END, END + 3 * DAY, as_of=at)
        self.assertEqual([(r["opponent"]["clan_tag"], r["missed_attacks"]) for r in result["rows"]],
                         [("#CU", None), ("#GR", None)])
        causes = {war["war_id"]: war["causes"] for war in result["coverage"]["incomplete_wars"]}
        self.assertEqual(sorted(causes.values()), [["lineup_incomplete", "attacks_incomplete"], ["not_final"]])
        self.assertFalse(result["coverage"]["complete"])
        rows = (await self.reads.participation(["#PY"], END, END + 3 * DAY, as_of=at))["rows"]
        self.assertEqual([(r["player_tag"], r["attacks_used"], r["missed"], r["final"]) for r in rows],
                         [("#P0", 1, None, False), ("#P2", 0, None, False)])

    async def test_final_counts_without_every_attack_still_give_known_misses(self):
        # Early copied wars list each account's final count but not where it stands on the map.
        early = war("warEnded")
        for member in early["clan"]["members"]:
            member.pop("townhallLevel")
        await self.record(early, at=END + 90 * SECOND)
        at = END + DAY
        rows = (await self.reads.participation(["#PY"], END, END + DAY, as_of=at))["rows"]
        self.assertEqual([(r["player_tag"], r["missed"], r["attacks_complete"]) for r in rows],
                         [("#P0", 1, True), ("#P2", 1, True)])
        # One attack is missing from the saved list, but the accounts' used attacks still add up to the clan's total.
        await self.execute("DELETE FROM war_attacks WHERE attack_order=1")
        rows = (await self.reads.participation(["#PY"], END, END + DAY, as_of=at))["rows"]
        self.assertEqual([(r["player_tag"], r["missed"], r["attacks_complete"]) for r in rows],
                         [("#P0", 1, False), ("#P2", 1, False)])
        # A total the copy summed from those same counts proves nothing.
        await self.execute("UPDATE war_sides SET extra_json='{\"derived\": [\"attacks\"]}' WHERE clan_tag='#PY'")
        rows = (await self.reads.participation(["#PY"], END, END + DAY, as_of=at))["rows"]
        self.assertEqual([r["missed"] for r in rows], [None, None])
        await self.execute("UPDATE war_sides SET extra_json='{}' WHERE clan_tag='#PY'")
        # The clan's total counts an attack no account holds: only an account with every attack used is final.
        await self.execute("UPDATE war_members SET attacks_used=2 WHERE player_tag='#P0'")
        await self.execute("UPDATE war_sides SET attacks=4 WHERE clan_tag='#PY'")
        rows = (await self.reads.participation(["#PY"], END, END + DAY, as_of=at))["rows"]
        self.assertEqual([(r["player_tag"], r["missed"]) for r in rows], [("#P0", 0), ("#P2", None)])
        self.assertIsNone((await self.reads.completed(["#PY"], END, END + DAY, as_of=at))["rows"][0]["missed_attacks"])
        # Before the war ends nothing is final, not even a full account.
        await self.execute("UPDATE wars SET state='inWar'")
        rows = (await self.reads.participation(["#PY"], END, END + DAY, as_of=at))["rows"]
        self.assertEqual([r["missed"] for r in rows], [None, None])

    async def test_participation_counts_hits_stars_triples_and_groups_by_owner(self):
        await self.links(END, link(100))
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        at = END + DAY
        rows = (await self.reads.participation(["#PY"], END, END + DAY, as_of=at))["rows"]
        self.assertEqual([(r["player_tag"], r["attacks_used"], r["attacks_expected"], r["missed"], r["stars"],
                           r["triples"], r["destruction"], r["attacks_measured"]) for r in rows],
                         [("#P0", 1, 2, 1, 3, 1, 100.0, 1), ("#P2", 1, 2, 1, 3, 1, 100.0, 1)])
        only = await self.reads.participation(["#PY"], END, END + DAY, player_tags=["#P2"], as_of=at)
        self.assertEqual([r["player_tag"] for r in only["rows"]], ["#P2"])
        grouped = await self.reads.participation(["#PY"], END, END + DAY, group_by="owner", as_of=at)
        self.assertEqual([(g["unit"], g["member_id"], g["player_tags"], g["wars"], g["missed"], g["totals_complete"])
                          for g in grouped["rows"]],
                         [("account", None, ["#P2"], 1, 1, True), ("member", 100, ["#P0"], 1, 1, True)])
        self.assertEqual(grouped["ownership_basis"], "owner at as_of")
        # Before the link existed the account was unowned.
        early = await self.reads.participation(["#PY"], END, END + DAY, group_by="owner", as_of=END - 1)
        self.assertEqual(early["rows"], [])
        # One unknown miss makes the owner's total unknown, never smaller.
        unfinished = war("inWar", clans=("#PY", "#CU"), end="20261030T180000.000Z")
        unfinished.update(preparationStartTime="20261028T180000.000Z", startTime="20261029T180000.000Z")
        await self.record(unfinished, at=END + DAY + HOUR)
        wide = await self.reads.participation(["#PY"], END, END + 3 * DAY, group_by="owner", as_of=END + 5 * DAY)
        member = next(g for g in wide["rows"] if g["unit"] == "member")
        self.assertEqual((member["wars"], member["opportunities"], member["attacks_used"], member["missed"],
                          member["totals_complete"]), (2, 2, 2, None, False))

    async def test_attacks_carry_both_town_halls_positions_and_freshness(self):
        repeated = war("warEnded")
        repeated["clan"]["members"][1]["attacks"][0]["defenderTag"] = "#Q0"
        repeated["opponent"]["members"][1].pop("bestOpponentAttack")
        await self.record(repeated, at=END + 90 * SECOND)
        at = END + DAY
        attacks = (await self.reads.attacks(["#PY"], END, END + DAY, as_of=at))["rows"]
        self.assertEqual([(a["attack_order"], a["attacker_tag"], a["defender_tag"], a["attacker_townhall"],
                           a["defender_townhall"], a["defender_position"], a["stars"], a["fresh"]) for a in attacks],
                         [(1, "#P0", "#Q0", 16, 16, 1, 3, True), (3, "#P2", "#Q0", 16, 16, 1, 3, False)])
        defenses = (await self.reads.attacks(["#PY"], END, END + DAY, direction="defenses", as_of=at))["rows"]
        self.assertEqual([(a["attack_order"], a["attacker_tag"], a["defender_tag"], a["defender_position"])
                          for a in defenses], [(2, "#Q0", "#P0", 1), (4, "#Q2", "#P2", 2)])
        selected = await self.reads.attacks(["#PY"], END, END + DAY, player_tags=["#P2"], as_of=at)
        self.assertEqual([a["attack_order"] for a in selected["rows"]], [3])

    async def test_attacks_without_the_opponent_lineup_leave_defenders_unknown(self):
        partial = war("warEnded")
        partial["opponent"].pop("members")
        await self.record(partial, at=END + 90 * SECOND)
        attacks = (await self.reads.attacks(["#PY"], END, END + DAY, as_of=END + DAY))
        self.assertEqual([(a["defender_tag"], a["defender_townhall"], a["defender_position"], a["fresh"])
                          for a in attacks["rows"]], [("#Q0", None, None, True), ("#Q2", None, None, True)])
        defenses = await self.reads.attacks(["#PY"], END, END + DAY, direction="defenses", as_of=END + DAY)
        self.assertEqual(defenses["rows"], [])
        self.assertFalse(defenses["coverage"]["complete"])

    async def test_log_results_count_log_entries_once_and_keep_private_apart(self):
        october = "20261010T080000.000Z"
        await self.record(page(log_entry("#GR", "20261020T180000.000Z", "win"),
                               log_entry("#CU", "20261015T180000.000Z", "lose"), league_week(october),
                               log_entry("#LQ", "20260930T180000.000Z", "win")),
                          kind="war_log", at=NOVEMBER + HOUR)
        # A league round is never a log entry; only the week's summary counts.
        await self.record(war("warEnded", clans=("#PY", "#CU"), end=october), kind="cwl_war", scope="#R0",
                          at=NOVEMBER + HOUR)
        counted = await self.reads.log_results("#PY", "2026-10", NOVEMBER + 2 * HOUR)
        self.assertEqual({key: counted[key] for key in ("status", "wins", "losses", "ties", "total", "unknown_result")},
                         {"status": "available", "wins": 2, "losses": 1, "ties": 0, "total": 3, "unknown_result": 0})
        self.assertEqual([entry["kind"] for entry in counted["entries"]], ["war", "war", "league_week"])
        self.assertEqual((counted["after"], counted["before"]), (OCTOBER, NOVEMBER))
        self.assertTrue(counted["coverage"]["read_after_month"])
        self.assertTrue(counted["coverage"]["complete"])
        empty = await self.reads.log_results("#PY", "2026-08", NOVEMBER + 2 * HOUR)
        self.assertEqual((empty["status"], empty["total"]), ("available", 0))
        # Read before the month ended, the count may still grow.
        self.assertFalse((await self.reads.log_results("#PY", "2026-11", NOVEMBER + 2 * HOUR))["coverage"]["complete"])
        await self.record({"reason": "accessDenied"}, kind="war_log", scope="#LQ", status=403, at=NOVEMBER)
        private = await self.reads.log_results("#LQ", "2026-10", NOVEMBER + HOUR)
        self.assertEqual((private["status"], private["total"], private["entries"]), ("private_log", None, []))
        self.assertEqual((await self.reads.log_results("#GR", "2026-10", NOVEMBER))["status"], "unavailable")

    async def test_log_results_keep_the_old_newest_fifty_meaning(self):
        letters = "0289PYLQGRJCUV"
        newer = [log_entry(f"#{letters[i // 14]}{letters[i % 14]}V", f"202611{i // 2 + 2:02d}T{6 + i % 2:02d}0000.000Z", "win")
                 for i in range(50)]
        await self.record(page(*newer, log_entry("#GR", "20261020T180000.000Z", "lose")), kind="war_log",
                          at=api_time("20261201T000000.000Z"))
        result = await self.reads.log_results("#PY", "2026-10", api_time("20261201T000000.000Z"))
        self.assertEqual((result["total"], result["beyond_log_window"]["losses"]), (0, 1))
        self.assertEqual((await self.reads.log_results("#PY", "2026-11", api_time("20261201T000000.000Z")))["wins"], 50)

    async def test_health_views_keep_capture_time_and_completed_meanings(self):
        await self.record(war("preparation"), at=START - HOUR)
        await self.record(war(), at=self.at)
        capture = await self.reads.health_activity(["#PY"], START, self.at, as_of=self.at)
        self.assertEqual([(r["player_tag"], r["state"], r["time"], r["time_basis"], r["attacks_used"],
                           r["attacks_expected"], r["stars"]) for r in capture["rows"]],
                         [("#P0", "inWar", self.at, "last_check", 1, 2, 3), ("#P2", "inWar", self.at, "last_check", 0, 2, 0)])
        self.assertEqual(capture["bounds"], "inclusive")
        self.assertEqual((await self.reads.health_activity(["#PY"], START, self.at - 1, as_of=self.at))["rows"], [])
        self.assertEqual((await self.reads.health_activity(["#PY"], START, END + DAY, view="completed",
                                                           as_of=self.at))["rows"], [])
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.record(friendly(), at=END + 3 * DAY)
        at = END + 4 * DAY
        ended = await self.reads.health_activity(["#PY"], END, END + 3 * DAY, view="completed", as_of=at)
        self.assertEqual({(r["time"], r["time_basis"], r["type"]) for r in ended["rows"]}, {(END, "war_end", "REG")})
        self.assertEqual((await self.reads.health_activity(["#PY"], START, END, view="completed", as_of=at))["rows"], [])
        self.assertEqual(len((await self.reads.health_activity(["#PY"], START, END, as_of=at))["rows"]), 2)
        with_friendly = await self.reads.health_activity(["#PY"], END, END + 3 * DAY, types=["REG", "FRIENDLY"], as_of=at)
        self.assertEqual(sorted({r["type"] for r in with_friendly["rows"]}), ["FRIENDLY", "REG"])
        # League wars are read only when asked for; there are none here.
        self.assertEqual((await self.reads.health_activity(["#PY"], END, END + DAY, types=["CWL"], as_of=at))["rows"], [])
        with self.assertRaises(ValueError):
            await self.reads.health_activity(["#PY"], END, END + DAY, types=["ARRANGED"])

    async def test_health_totals_sum_each_account_over_one_snapshot_and_keep_unknown_counts_apart(self):
        await self.record(war(), at=self.at)
        # The ongoing war counts as last captured, its attacks so far included.
        capture = await self.reads.health_totals(["#PY"], START, self.at, player_tags=["#P0", "#P2", "#Q0"], as_of=self.at)
        self.assertEqual([(r["player_tag"], r["wars"], r["attacks_expected"], r["attacks_used"], r["stars"],
                           r["attacks_measured"], r["wars_without_counts"]) for r in capture["rows"]],
                         [("#P0", 1, 2, 1, 3, 1, 0), ("#P2", 1, 2, 0, 0, 0, 0)])
        self.assertEqual((capture["view"], capture["bounds"], capture["coverage"]["wars_selected"]), ("capture", "inclusive", 1))
        self.assertEqual((await self.reads.health_totals(["#PY"], START, self.at, view="completed",
                                                         player_tags=["#P0"], as_of=self.at))["rows"], [])
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.record(friendly(), at=END + 3 * DAY)
        at = END + 4 * DAY
        ended = await self.reads.health_totals(["#PY"], START, at, player_tags=["#P0"], as_of=at)
        self.assertEqual([(r["wars"], r["attacks_expected"], r["attacks_used"], r["triples"], r["destruction"])
                          for r in ended["rows"]], [(1, 2, 1, 1, 100.0)])
        # A war that does not know a count adds the war, never a used or missed attack.
        await self.execute("UPDATE war_members SET attacks_expected=NULL WHERE player_tag='#P0'")
        (unknown,) = (await self.reads.health_totals(["#PY"], START, at, player_tags=["#P0"], as_of=at))["rows"]
        self.assertEqual((unknown["wars"], unknown["wars_without_counts"], unknown["attacks_expected"],
                          unknown["attacks_used"]), (1, 1, 0, 0))
        # Friendly wars are counted only when asked for.
        both = await self.reads.health_totals(["#PY"], START, at, player_tags=["#P2"], types=["REG", "FRIENDLY"], as_of=at)
        self.assertEqual(both["rows"][0]["wars"], 2)
        counts = await self.reads.health_war_counts(["#PY", "#LQ"], START, at, as_of=at)
        self.assertEqual(counts["clans"], {"#PY": {"wars": 1, "wars_with_lineup": 1},
                                           "#LQ": {"wars": 0, "wars_with_lineup": 0}})
        self.assertEqual((await self.reads.health_war_counts(["#PY"], START, at, types=["REG", "FRIENDLY"],
                                                             as_of=at))["clans"]["#PY"]["wars"], 2)
        self.assertEqual((await self.reads.health_war_counts(["#PY"], START, at, types=["CWL"],
                                                             as_of=at))["clans"]["#PY"]["wars"], 0)
        # A war known only from the log has no lineup to count.
        await self.execute("DELETE FROM war_members WHERE war_id=(SELECT war_id FROM war_sides WHERE clan_tag='#PY' "
                           "AND war_id IN (SELECT id FROM wars WHERE type='REG'))")
        self.assertEqual((await self.reads.health_war_counts(["#PY"], START, at, as_of=at))["clans"]["#PY"],
                         {"wars": 1, "wars_with_lineup": 0})
        for call in (lambda: self.reads.health_totals(["#PY"], START, END, player_tags=[]),
                     lambda: self.reads.health_totals(["#PY"], START, END, player_tags=["#P0"], view="live"),
                     lambda: self.reads.health_war_counts(["#PY"], START, END, types=["ARRANGED"])):
            with self.assertRaises(ValueError):
                await call()

    async def test_a_copied_attack_without_an_opponent_lineup_shows_the_old_capture_of_its_defender(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        at = END + DAY
        # The record places the defender by its own lineup; the old capture fills the rest.
        legacy = ('{"attacker_tag":"#P0","defender_tag":"#Q0","legacy":{"defender_name":"Old","defender_position":4,'
                  '"defender_townhall":15,"fresh_attack":true},"stub":false}')
        await self.execute("UPDATE war_attacks SET defender_member_id=NULL,extra_json=? WHERE attack_order=1", (legacy,))
        # The clan's attack count is unknown, so the record cannot itself call a first hit.
        await self.execute("UPDATE war_sides SET attacks=NULL WHERE clan_tag='#PY'")
        rows = (await self.reads.attacks(["#PY"], END, END + DAY, as_of=at))["rows"]
        self.assertEqual([(a["attack_order"], a["defender_tag"], a["defender_name"], a["defender_townhall"],
                           a["defender_position"], a["defender_basis"], a["fresh"], a["fresh_basis"]) for a in rows],
                         [(1, "#Q0", "Old", 15, 1, "old_capture", True, "old_capture"),
                          (3, "#Q2", "Player #Q2", 16, 2, "lineup", None, "attacks")])
        # A defender the record holds without a town hall takes the old capture's; the lineup is
        # then incomplete, so no position is known except the old capture's.
        await self.execute("UPDATE war_members SET townhall=NULL WHERE player_tag='#Q2'")
        await self.execute("UPDATE war_attacks SET extra_json=? WHERE attack_order=3", (
            '{"legacy":{"defender_townhall":14,"defender_position":3,"fresh_attack":false},"stub":false}',))
        row = (await self.reads.attacks(["#PY"], END, END + DAY, as_of=at))["rows"][1]
        self.assertEqual((row["defender_name"], row["defender_townhall"], row["defender_position"], row["defender_basis"],
                          row["fresh"], row["fresh_basis"]), ("Player #Q2", 14, 3, "old_capture", False, "old_capture"))

    async def fresh_by_order(self):
        rows = (await self.reads.attacks(["#PY"], END, END + DAY, as_of=END + DAY))["rows"]
        return {row["attack_order"]: row["fresh"] for row in rows}

    async def test_a_first_hit_before_the_first_unsaved_attack_is_still_proven(self):
        # Clash numbers both sides' attacks in one sequence: the clan's 1 and 3, the opponent's 2 and 4.
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        self.assertEqual(await self.fresh_by_order(), {1: True, 3: True})
        await self.execute("DELETE FROM war_attacks WHERE attack_order=3")
        self.assertEqual(await self.fresh_by_order(), {1: True})
        # Without the opponent's count, the missing number cannot be found.
        await self.execute("UPDATE war_sides SET attacks=NULL WHERE clan_tag='#GR'")
        self.assertEqual(await self.fresh_by_order(), {1: None})

    async def test_an_attack_after_an_unsaved_one_stays_unknown(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.execute("DELETE FROM war_attacks WHERE attack_order=1")
        self.assertEqual(await self.fresh_by_order(), {3: None})

    async def test_a_saved_attack_without_a_defender_is_a_gap(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        # The clan made a third attack, numbered 5, that is not saved.
        await self.execute("UPDATE war_sides SET attacks=3 WHERE clan_tag='#PY'")
        self.assertEqual(await self.fresh_by_order(), {1: True, 3: True})
        # Attack 2 could then have been on attack 3's defender.
        await self.execute("UPDATE war_attacks SET defender_member_id=NULL WHERE attack_order=2")
        self.assertEqual(await self.fresh_by_order(), {1: True, 3: None})

    async def test_parity_rehearsal_matches_old_wars_and_explains_differences(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        path = self.path.parent / "clan_health.db"
        end = END // SECOND
        with closing(sqlite3.connect(path)) as old:
            create_schema(old)
            for war_id, opponent in (("REG:#PY:a:#GR", "#GR"), ("REG:#PY:b:#CU", "#CU")):
                old.execute("INSERT INTO wars(war_id,war_type,clan_code,clan_tag,opponent_tag,team_size,attacks_per_member,"
                            "state,end_ts,last_seen_ts) VALUES (?,'REG','ONE','#PY',?,2,2,'warEnded',?,?)",
                            (war_id, opponent, end if opponent == "#GR" else end - 3 * 86400, end))
            for order, tag, defender, used in ((1, "#P0", "#Q0", 1), (3, "#P2", "#Q2", 2)):
                old.execute("INSERT INTO war_roster_members(war_id,clan_code,player_tag,player_name,attacks_expected,"
                            "attacks_used,roster_state,captured_ts) VALUES ('REG:#PY:a:#GR','ONE',?,'Old',2,?,'warEnded',?)",
                            (tag, used, end))
                old.execute("INSERT INTO war_attacks(war_id,war_type,clan_code,clan_tag,end_ts,war_state,player_tag,"
                            "player_name,attack_order,defender_tag,stars) VALUES ('REG:#PY:a:#GR','REG','ONE','#PY',?,"
                            "'warEnded',?,'Old',?,?,3)", (end, tag, order, defender))
            old.commit()
        result = await compare_frozen_wars(self.reads, path, {"ONE": "#PY"}, as_of=END + DAY)
        self.assertEqual([row["old_war_id"] for row in result["matched"]], ["REG:#PY:a:#GR"])
        self.assertEqual([(row["old_war_id"], row["reason"]) for row in result["only_old"]],
                         [("REG:#PY:b:#CU", "not in record")])
        self.assertEqual([(d["field"], d["old"], d["new"], d["reason"]) for d in result["differences"]],
                         [("clan_attacks_used", 3, 2, "unexplained"), ("#P2/attacks_used", 2, 1, "unexplained")])
        self.assertEqual(result["only_record"], [])

    async def test_arguments_are_bounded_and_explicit(self):
        for call in (lambda: self.reads.completed(["#PY"], None, END),
                     lambda: self.reads.completed(["#PY"], END, END),
                     lambda: self.reads.completed([], START, END),
                     lambda: self.reads.completed(["#PY"], START, END, types=["OTHER"]),
                     lambda: self.reads.completed(["#PY"], START, END, limit=201),
                     lambda: self.reads.participation(["#PY"], START, END, group_by="clan"),
                     lambda: self.reads.attacks(["#PY"], START, END, direction="both"),
                     lambda: self.reads.health_activity(["#PY"], START, END, view="live"),
                     lambda: self.reads.log_results("#PY", "2026-13"),
                     lambda: self.reads.log_results("#PY", "2026-1"),
                     lambda: self.reads.current("not a tag")):
            with self.assertRaises(ValueError):
                await call()


if __name__ == "__main__":
    unittest.main()
