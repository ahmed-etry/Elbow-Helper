"""The regular-war import: all-or-nothing, repeatable, alias-keeping and never over the record's own facts."""

from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import unittest
from unittest.mock import patch

from elbow_helper.data.reads import WarReads
from elbow_helper.data.store import wars_import
from elbow_helper.data.store.legacy_links import ImportMismatch
from elbow_helper.data.store.wars_import import import_wars, snapshot_wars
from elbow_helper.data.store.war_projection import HOUR
from elbow_helper.features.clan_health.attendance import _participation_only_wars
from elbow_helper.features.clan_health.database.schema import create_schema

from .test_wars import END, SECOND, START, WarFixture, war, war_log


DAY = 24 * HOUR
END_S = END // SECOND
CLANS = {"ONE": "#PY", "TWO": "#LQ"}
FINAL, LIVE = "currentwar_api:warEnded", "currentwar_api:inWar"


def old_id(end_s, tag="#PY", opponent="#GR"):
    return f"REG:{tag}:{datetime.fromtimestamp(end_s, timezone.utc).strftime('%Y%m%dT%H%M%S.000Z')}:{opponent}"


def old_war(c, war_id, *, code="ONE", tag="#PY", opponent="#GR", state="warEnded", end=END_S, kind="REG",
            times=True, team=2, seen=None):
    c.execute("INSERT INTO wars(war_id,war_type,clan_code,clan_tag,opponent_tag,opponent_name,team_size,"
              "attacks_per_member,state,preparation_start_ts,start_ts,end_ts,last_seen_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (war_id, kind, code, tag, opponent, "Other", team, 2, state, end - 2 * 86400 if times else 0,
               end - 86400 if times else 0, end, seen or end + 100))


def old_lineup(c, war_id, players, *, code="ONE", tag="#PY", state="warEnded", at=END_S + 60, source=FINAL):
    for position, (player, used) in enumerate(players, 1):
        c.execute("INSERT INTO war_roster_members(war_id,clan_code,player_tag,player_name,townhall,map_position,"
                  "attacks_expected,attacks_used,roster_state,captured_ts,source) VALUES (?,?,?,?,?,?,2,?,?,?,?)",
                  (war_id, code, player, "Old", 16, position, used, state, at, source))
        c.execute("INSERT INTO war_activity(war_id,war_type,clan_code,clan_tag,end_ts,player_tag,player_name,"
                  "attacks_expected,attacks_used,stars,destruction,attack_count,source) VALUES (?,'REG',?,?,?,?,'Old',2,?,3,100,?,?)",
                  (war_id, code, tag, END_S, player, used, used, source))


def old_attacks(c, war_id, attacks, *, code="ONE", tag="#PY", end=END_S, state="warEnded", kind="REG"):
    for order, attacker, defender, stars in attacks:
        c.execute("INSERT INTO war_attacks(war_id,war_type,clan_code,clan_tag,end_ts,war_state,player_tag,player_name,"
                  "attack_order,defender_tag,defender_name,defender_map_position,defender_townhall,stars,destruction,"
                  "fresh_attack,duration,source) VALUES (?,?,?,?,?,?,?,'Old',?,?,'Them',1,16,?,100,1,100,?)",
                  (war_id, kind, code, tag, end, state, attacker, order, defender, stars, FINAL))


def old_activity(c, war_id, players, *, code="ONE", tag="#PY", source=FINAL, end=END_S):
    for player, used in players:
        c.execute("INSERT INTO war_activity(war_id,war_type,clan_code,clan_tag,end_ts,player_tag,player_name,"
                  "attacks_expected,attacks_used,stars,destruction,attack_count,source) VALUES (?,'REG',?,?,?,?,'Old',2,?,3,100,?,?)",
                  (war_id, code, tag, end, player, used, used, source))


class ImportFixture(WarFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.health = self.path.parent / "clan_health.db"
        self.cache = None
        with closing(sqlite3.connect(self.health)) as old:
            create_schema(old)
            old.commit()

    def old(self):
        return closing(sqlite3.connect(self.health))

    async def run_import(self, **kwargs):
        if "frozen" not in kwargs:
            # Frozen after every war here, so no old capture lies in the future.
            kwargs["frozen"] = snapshot_wars(self.health, self.cache, END + 30 * DAY)
        return await self.store.call(import_wars, self.health, self.cache, clans=CLANS, **kwargs)

    async def dump(self):
        return await self.store.call(lambda c: tuple(c.iterdump()))

    async def regular_war(self):
        """The record's war log copy, with the old store's lineup and attacks for the same war."""
        await self.record(war_log(), kind="war_log")
        with self.old() as old, old:
            old_war(old, old_id(END_S))
            old_lineup(old, old_id(END_S), [("#P0", 1), ("#P2", 1)])
            old_attacks(old, old_id(END_S), [(1, "#P0", "#Q0", 3), (3, "#P2", "#Q2", 2)])
        return old_id(END_S)


class ImportTests(ImportFixture, unittest.IsolatedAsyncioTestCase):
    async def test_old_lineups_and_attacks_join_the_logged_war_and_every_old_id_stays(self):
        old_war_id = await self.regular_war()
        with self.old() as old, old:
            # League rows are counted out, never imported.
            old_war(old, "CWL:#R0", kind="CWL")
            old_lineup(old, "CWL:#R0", [("#P0", 1)])
            old_attacks(old, "CWL:#R0", [(1, "#P0", "#Q0", 3)], kind="CWL")
            old.execute("UPDATE war_activity SET war_type='CWL' WHERE war_id='CWL:#R0'")
        before = await self.rows("SELECT id FROM wars")
        result = await self.run_import()
        self.assertTrue(result["imported"])
        self.assertEqual(await self.rows("SELECT id FROM wars"), before)
        checks = result["checks"]
        self.assertEqual(checks["outcomes"]["wars"], {"excluded:cwl": 1, "matched": 1})
        self.assertEqual(checks["outcomes"]["war_roster_members"], {"excluded:cwl": 1, "imported": 2})
        self.assertEqual(checks["outcomes"]["war_activity"], {"excluded:cwl": 1, "imported": 2})
        self.assertEqual(checks["outcomes"]["war_attacks"], {"excluded:cwl": 1, "imported": 2})
        self.assertEqual((checks["wars"]["created"], checks["wars"]["matched_existing"]), (0, {"end": 1}))
        self.assertEqual(checks["clans"]["ONE"]["lineup_rows"], 2)
        self.assertEqual(checks["clans"]["ONE"]["aliases"], 1)
        # What the log copy lacked is filled; the logged result stays.
        war_row = (await self.rows("SELECT * FROM wars"))[0]
        self.assertEqual((war_row["preparation_at"], war_row["start_at"], war_row["attacks_per_member"], war_row["type_basis"]),
                         (END - 2 * DAY, END - DAY, 2, "war_log"))
        self.assertEqual(len(await self.rows("SELECT * FROM war_source_keys WHERE source_kind='preparation'")), 1)
        alias = (await self.rows("SELECT * FROM war_legacy_ids"))[0]
        self.assertEqual((alias["source"], alias["legacy_id"], alias["clan_code"], alias["war_id"], alias["legacy_label"],
                          alias["capture_state"], alias["captured_at"]), ("clan_health", old_war_id, "ONE", war_row["id"], "REG",
                                                                        "warEnded", (END_S + 100) * SECOND))
        members = await self.rows("SELECT player_tag,side,townhall,attacks_expected,attacks_used FROM war_members ORDER BY player_tag")
        self.assertEqual([(m["player_tag"], m["side"], m["attacks_used"]) for m in members], [("#P0", 1, 1), ("#P2", 1, 1)])
        attacks = await self.rows("SELECT attack_order,stars,json_extract(extra_json,'$.defender_tag') AS defender,"
                                  "json_extract(extra_json,'$.legacy.fresh_attack') AS fresh FROM war_attacks ORDER BY attack_order")
        self.assertEqual([(a["attack_order"], a["stars"], a["defender"], a["fresh"]) for a in attacks],
                         [(1, 3, "#Q0", 1), (3, 2, "#Q2", 1)])
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])
        # The record's own reads see the imported lineup.
        people = await WarReads(self.store).participation(["#PY"], START, END + DAY, as_of=END + 3 * DAY)
        self.assertEqual([(r["player_tag"], r["attacks_used"], r["missed"], r["stars"]) for r in people["rows"]],
                         [("#P0", 1, 1, 3), ("#P2", 1, 1, 2)])

    async def test_a_repeat_adds_nothing_even_without_its_marker(self):
        await self.regular_war()
        await self.run_import()
        settled = await self.dump()
        again = await self.run_import()
        self.assertFalse(again["imported"])
        self.assertEqual(await self.dump(), settled)
        # Without the marker the same checked copy runs again and still finds nothing to add.
        await self.execute("DELETE FROM imports WHERE topic='wars_regular'")
        runs = len(await self.rows("SELECT * FROM collection_runs"))
        rerun = await self.run_import()
        self.assertTrue(rerun["imported"])
        self.assertEqual(rerun["checks"]["inserted"], {"lineup_rows": 0, "attacks": 0})
        self.assertEqual(rerun["checks"]["clans"]["ONE"]["aliases_added"], 0)
        self.assertEqual(len(await self.rows("SELECT * FROM collection_runs")), runs + 1)
        tables = ("wars", "war_sides", "war_members", "war_attacks", "war_legacy_ids", "war_source_keys")
        for table in tables:
            self.assertEqual([line for line in settled if f'INSERT INTO "{table}"' in line],
                             [line for line in await self.dump() if f'INSERT INTO "{table}"' in line], table)

    async def test_a_bad_row_or_a_failed_check_commits_nothing(self):
        await self.regular_war()
        before = await self.dump()
        for statement, reason in (
                ("UPDATE war_attacks SET stars=7", "bad_row:war_attacks"),
                ("UPDATE war_roster_members SET player_tag='not a tag' WHERE player_tag='#P0'", "bad_row:war_roster_members"),
                ("UPDATE wars SET clan_tag='#ZZ'", "bad_row:wars"),
                ("UPDATE wars SET state='over'", "bad_row:wars"),
                ("UPDATE wars SET war_type='SIDE'", "bad_row:wars")):
            with self.subTest(reason=reason, statement=statement):
                with self.old() as old, old:
                    old.execute(statement)
                with self.assertRaisesRegex(ImportMismatch, reason):
                    await self.run_import()
                self.assertEqual(await self.dump(), before)
                with self.old() as old, old:
                    old.executescript("DELETE FROM wars; DELETE FROM war_roster_members; DELETE FROM war_attacks; "
                                      "DELETE FROM war_activity;")
                    old_war(old, old_id(END_S))
                    old_lineup(old, old_id(END_S), [("#P0", 1), ("#P2", 1)])
                    old_attacks(old, old_id(END_S), [(1, "#P0", "#Q0", 3), (3, "#P2", "#Q2", 2)])
        self.assertEqual(await self.rows("SELECT * FROM imports"), [])

    async def test_a_failure_after_writing_rolls_everything_back(self):
        await self.regular_war()
        with self.old() as old, old:
            # A second war whose alias already points at a war of another pair stops the copy after the first one is written.
            old_war(old, old_id(END_S - 5 * 86400), end=END_S - 5 * 86400)
            old_lineup(old, old_id(END_S - 5 * 86400), [("#P0", 1)])
        await self.record(war("warEnded", clans=("#PY", "#CU")), at=END + 120 * SECOND)
        other = (await self.rows("SELECT w.id FROM wars w JOIN war_sides s ON s.war_id=w.id WHERE s.clan_tag='#CU'"))[0]["id"]
        await self.execute("INSERT INTO war_legacy_ids VALUES ('clan_health',?,'ONE',?,'REG','warEnded',NULL,1)",
                           (old_id(END_S - 5 * 86400), other))
        before = await self.dump()
        with self.assertRaisesRegex(ImportMismatch, "war_pair_mismatch"):
            await self.run_import()
        self.assertEqual(await self.dump(), before)
        # A source that changes while the copy runs, and a check that fails, leave nothing either.
        await self.execute("DELETE FROM war_legacy_ids")
        before = await self.dump()
        real = snapshot_wars(self.health)
        changed = real.__class__(real.version, "changed", real.frozen_at, real.rows, real.history)
        with patch.object(wars_import, "snapshot_wars", lambda *args, **kwargs: changed):
            with self.assertRaisesRegex(ImportMismatch, "legacy_changed_during_import"):
                await self.run_import(frozen=real)
        self.assertEqual(await self.dump(), before)
        with patch.object(wars_import.Importer, "verify", side_effect=ImportMismatch("lineup_mismatch")):
            with self.assertRaisesRegex(ImportMismatch, "lineup_mismatch"):
                await self.run_import()
        self.assertEqual(await self.dump(), before)

    async def test_unreadable_sources_stop_the_copy(self):
        await self.regular_war()
        before = await self.dump()
        self.cache = self.path.parent / "war_cache.json"
        for content, reason in ((b"{not json", "cache_unreadable"), (b'{"processed_wars": []}', "cache_unreadable"),
                                (b'{"war_board_history": []}', "cache_unreadable")):
            with self.subTest(reason=reason):
                self.cache.write_bytes(content)
                with self.assertRaisesRegex(ImportMismatch, reason):
                    await self.run_import()
        self.cache = self.path.parent / "missing.json"
        with self.assertRaisesRegex(ImportMismatch, "cache_unreadable"):
            await self.run_import()
        self.cache = None
        with self.old() as old, old:
            old.execute("DROP TABLE war_attacks")
        with self.assertRaisesRegex(ImportMismatch, "legacy_schema_mismatch:war_attacks"):
            await self.run_import()
        self.health.write_bytes(b"not a database")
        with self.assertRaisesRegex(ImportMismatch, "legacy_unreadable"):
            await self.run_import()
        self.assertEqual(await self.dump(), before)

    async def test_paused_wars_and_repeated_ids_are_one_war_with_every_id_kept(self):
        # The record only knows the war's logged end, hours after the end the old store first saw.
        await self.record(war_log(war("warEnded", end="20261028T213000.000Z")), kind="war_log")
        first, second = old_id(END_S), old_id(END_S + 1800)
        with self.old() as old, old:
            old_war(old, first, state="inWar")
            old_lineup(old, first, [("#P0", 0)], state="inWar", source=LIVE)
            old_war(old, second, end=END_S + 1800 - 3600 * 0)
            old.execute("UPDATE wars SET preparation_start_ts=?,start_ts=?,end_ts=? WHERE war_id=?",
                        (END_S - 2 * 86400, END_S - 86400, END_S + 1800, second))
            old.execute("UPDATE wars SET preparation_start_ts=?,start_ts=? WHERE war_id=?",
                        (END_S - 2 * 86400, END_S - 86400, first))
            old_lineup(old, second, [("#P0", 1), ("#P2", 1)])
        result = await self.run_import()
        wars = await self.rows("SELECT id FROM wars")
        self.assertEqual(len(wars), 1)
        self.assertEqual(result["checks"]["wars"]["matched_existing"], {"pause_window": 1})
        aliases = await self.rows("SELECT legacy_id,capture_state FROM war_legacy_ids ORDER BY legacy_id")
        self.assertEqual([(a["legacy_id"], a["capture_state"]) for a in aliases], sorted([(first, "inWar"), (second, "warEnded")]))
        # The later, final capture supplies the lineup; the earlier one adds nothing of its own.
        self.assertEqual(sorted(m["player_tag"] for m in await self.rows("SELECT player_tag FROM war_members")), ["#P0", "#P2"])
        self.assertEqual((await self.rows("SELECT attacks_used FROM war_members WHERE player_tag='#P0'"))[0]["attacks_used"], 1)
        self.assertEqual(result["checks"]["outcomes"]["war_roster_members"], {"excluded:older_capture": 1, "imported": 2})

    async def test_family_mirrors_and_the_cached_board_payload_are_one_war(self):
        payload = war("warEnded", clans=("#PY", "#LQ"))
        opponent = war("warEnded", clans=("#PY", "#LQ"))
        opponent["clan"], opponent["opponent"] = payload["opponent"], payload["clan"]
        self.cache = self.path.parent / "war_cache.json"
        self.cache.write_text(json.dumps({"processed_wars": ["ignored"], "war_board_history": {
            "ONE": {"current": payload, "previous": None}, "TWO": {"current": {"state": "notInWar"}, "previous": {}}}}))
        mirror_id = old_id(END_S, "#LQ", "#PY")
        with self.old() as old, old:
            for code, tag, opponent_tag, war_id in (("ONE", "#PY", "#LQ", old_id(END_S, "#PY", "#LQ")),
                                                     ("TWO", "#LQ", "#PY", mirror_id)):
                old_war(old, war_id, code=code, tag=tag, opponent=opponent_tag)
                old_lineup(old, war_id, [("#P0" if tag == "#PY" else "#Q0", 1)], code=code, tag=tag)
        sources = (self.health.read_bytes(), self.cache.read_bytes())
        result = await self.run_import()
        self.assertEqual((self.health.read_bytes(), self.cache.read_bytes()), sources)
        checks = result["checks"]
        self.assertEqual(await self.rows("SELECT type,type_basis,state FROM wars"), [
            {"type": "REG", "type_basis": "times", "state": "warEnded"}])
        self.assertEqual((checks["wars"]["events"], checks["wars"]["created"]), (1, 1))
        # The full payload is the base; the old lineup rows are matched by it, not added again.
        self.assertEqual(checks["outcomes"]["war_board_history"], {"excluded:empty": 2, "excluded:not_in_war": 1, "imported": 1})
        self.assertEqual(checks["outcomes"]["war_roster_members"], {"matched": 2})
        self.assertEqual(len(await self.rows("SELECT * FROM war_members")), 4)
        self.assertEqual(len(await self.rows("SELECT * FROM war_attacks")), 4)
        self.assertEqual(await self.rows("SELECT DISTINCT source,clan_code FROM war_legacy_ids ORDER BY source,clan_code"), [
            {"source": "clan_health", "clan_code": "ONE"}, {"source": "clan_health", "clan_code": "TWO"},
            {"source": "war_cache", "clan_code": "ONE"}])
        self.assertEqual(len(await self.rows("SELECT * FROM war_legacy_ids")), 3)
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])
        stored = (await self.rows("SELECT roster_complete,attacks_complete,result_complete,final_at FROM wars"))[0]
        self.assertEqual(tuple(stored.values())[:3], (1, 1, 1))
        # Both clans' mirrors, in the record's sorted sides.
        self.assertEqual([r["clan_tag"] for r in await self.rows("SELECT clan_tag FROM war_sides ORDER BY side")], ["#LQ", "#PY"])
        self.assertEqual(
            {m["player_tag"]: m["side"] for m in await self.rows("SELECT player_tag,side FROM war_members")},
            {"#P0": 1, "#P2": 1, "#Q0": 0, "#Q2": 0})

    async def test_early_wars_with_final_participation_only_are_found_the_way_attendance_finds_them(self):
        stored_end, early, other = END_S, END_S - 20 * 86400, END_S - 30 * 86400
        stored_id, paused = old_id(stored_end), old_id(stored_end + 3600)
        first, last = old_id(early, opponent="#CU"), old_id(early + 7200, opponent="#CU")
        unfinished = old_id(other, opponent="#LQ")
        with self.old() as old, old:
            old_war(old, stored_id)
            old_lineup(old, stored_id, [("#P0", 1)])
            # A late capture of the stored war is only one more ID for it.
            old_activity(old, paused, [("#P0", 1)], end=stored_end + 3600)
            old_activity(old, first, [("#P0", 1), ("#P2", 0)], source=LIVE, end=early)
            old_activity(old, last, [("#P0", 2), ("#P2", 1), ("#P8", 0)], end=early + 7200)
            old_attacks(old, first, [(1, "#P0", "#Q0", 1)], end=early)
            old_attacks(old, last, [(1, "#P0", "#Q0", 3), (2, "#P0", "#Q2", 2), (3, "#P2", "#Q8", 1)], end=early + 7200)
            old_attacks(old, last, [(4, "#P2", "#Q2", 1)], end=early + 7200, state="inWar")
            # Never captured after it ended, and an ID that cannot say its opponent.
            old_activity(old, unfinished, [("#P0", 1)], source=LIVE, end=other)
            old_attacks(old, unfinished, [(1, "#P0", "#Q0", 3)], end=other, state="inWar")
            old_activity(old, "REG:garbage", [("#P0", 1)], source=LIVE)
            old_attacks(old, "REG:orphan", [(1, "#P0", "#Q0", 3)])
        with closing(sqlite3.connect(self.health)) as old:
            old.row_factory = sqlite3.Row
            stored = [dict(r) for r in old.execute("SELECT * FROM wars WHERE state='warEnded'")]
            found, _ = _participation_only_wars(old, ["ONE"], stored)
        result = await self.run_import()
        checks = result["checks"]
        created = await self.rows("SELECT * FROM wars WHERE type_basis='legacy_unchecked'")
        self.assertEqual(len(created), len(found))
        self.assertEqual((created[0]["type"], created[0]["preparation_at"], created[0]["team_size"], created[0]["state"],
                          created[0]["end_at"]), ("REG", None, None, "warEnded", (early + 7200) * SECOND))
        ours = await self.rows("SELECT war_id,legacy_id FROM war_legacy_ids")
        grouped = {}
        for row in ours:
            grouped.setdefault(row["war_id"], set()).add(row["legacy_id"])
        # The same wars with the same IDs as the attendance finds, plus the stored war and its late capture.
        self.assertEqual({frozenset(info["aliases"]) for _, info in found},
                         {frozenset(grouped[row["id"]]) for row in created})
        self.assertIn(frozenset({stored_id, paused}), {frozenset(ids) for ids in grouped.values()})
        members = await self.rows("SELECT player_tag,attacks_used,townhall,source_map_position FROM war_members "
                                  "WHERE war_id=? ORDER BY player_tag", (created[0]["id"],))
        self.assertEqual([(m["player_tag"], m["attacks_used"], m["townhall"], m["source_map_position"]) for m in members],
                         [("#P0", 2, None, None), ("#P2", 1, None, None), ("#P8", 0, None, None)])
        self.assertEqual(len(await self.rows("SELECT * FROM war_attacks WHERE war_id=?", (created[0]["id"],))), 3)
        # Their own attack total is the sum of the final lineup, marked as derived.
        side = (await self.rows("SELECT attacks,extra_json FROM war_sides WHERE war_id=? AND clan_tag='#PY'", (created[0]["id"],)))[0]
        self.assertEqual((side["attacks"], json.loads(side["extra_json"])["derived"]), (3, ["attacks"]))
        self.assertEqual(checks["clans"]["ONE"]["excluded"]["war_activity"], {
            "covered_by_stored_war": 1, "no_final_capture": 1, "not_chosen_capture": 2, "unproven_identity": 1})
        self.assertEqual(checks["clans"]["ONE"]["excluded"]["war_attacks"], {
            "no_final_capture": 1, "no_lineup": 1, "not_chosen_capture": 1, "not_final_row": 1})
        # A war that was never captured after it ended gets no war and no alias.
        self.assertFalse(any(unfinished in ids for ids in grouped.values()))

    async def test_the_records_own_facts_are_never_overwritten(self):
        # A war the collector saw in full, and an older store that disagrees with it.
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        with self.old() as old, old:
            old_war(old, old_id(END_S), team=3)
            old_lineup(old, old_id(END_S), [("#P0", 0), ("#P2", 0), ("#P8", 2)])
            old_attacks(old, old_id(END_S), [(1, "#P0", "#Q0", 1), (9, "#P8", "#Q8", 3)])
        snapshots = ("wars", "war_sides", "war_members", "war_attacks")

        async def record_rows():
            return [await self.rows(f"SELECT * FROM {table}") for table in snapshots]
        before = await record_rows()
        result = await self.run_import()
        # Not one fact of the record changes: its war, sides, lineup and attacks read back exactly as before.
        self.assertEqual(await record_rows(), before)
        checks = result["checks"]
        self.assertEqual(checks["outcomes"]["war_roster_members"], {"conflict": 2, "kept_record": 1})
        self.assertEqual(checks["outcomes"]["war_attacks"], {"kept_record": 1, "matched": 1})
        self.assertEqual(checks["inserted"], {"lineup_rows": 0, "attacks": 0})
        listed = {(c.get("field"), c.get("player_tag")) for c in checks["record_conflicts"]}
        self.assertIn(("team_size", None), listed)
        self.assertIn((None, "#P0"), listed)
        self.assertEqual(len(await self.rows("SELECT * FROM war_legacy_ids")), 1)

    async def test_a_pair_the_record_cannot_tell_apart_stays_separate_and_flagged(self):
        # Two live copies of one pair and end make the old store's war ambiguous.
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        other = war("warEnded")
        other["preparationStartTime"] = "20261026T170000.000Z"
        await self.record(other, at=END + 120 * SECOND)
        with self.old() as old, old:
            # No preparation time to tell the two apart by.
            old_activity(old, old_id(END_S), [("#P0", 1)])
        result = await self.run_import()
        self.assertEqual((result["checks"]["wars"]["created"], result["checks"]["wars"]["ambiguous"]), (1, 1))
        flagged = await self.rows("SELECT war_id FROM war_source_keys WHERE source_kind='legacy_ambiguous'")
        self.assertEqual(len(flagged), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 3)
        self.assertEqual((await self.rows("SELECT war_id FROM war_legacy_ids"))[0]["war_id"], flagged[0]["war_id"])

    async def test_changed_sources_are_reconciled_by_the_same_checked_copy(self):
        await self.regular_war()
        later = old_id(END_S + 4 * 86400)
        with self.old() as old, old:
            old_war(old, later, state="inWar", end=END_S + 4 * 86400)
            old_lineup(old, later, [("#P0", 0)], state="inWar", source=LIVE)
        first = await self.run_import()
        self.assertEqual(first["checks"]["wars"]["created"], 1)
        created = (await self.rows("SELECT * FROM wars WHERE end_at=?", ((END_S + 4 * 86400) * SECOND,)))[0]
        self.assertEqual((created["state"], created["type_basis"]), ("inWar", "times"))
        # The old store keeps writing: the war ends, and a new war appears.
        newest = old_id(END_S + 8 * 86400)
        with self.old() as old, old:
            old.execute("UPDATE wars SET state='warEnded',last_seen_ts=last_seen_ts+500 WHERE war_id=?", (later,))
            old.execute("UPDATE war_roster_members SET roster_state='warEnded',attacks_used=2,captured_ts=captured_ts+500 WHERE war_id=?",
                        (later,))
            old.execute("UPDATE war_activity SET attacks_used=2,source=? WHERE war_id=?", (FINAL, later))
            old_war(old, newest, end=END_S + 8 * 86400)
            old_lineup(old, newest, [("#P2", 1)])
        second = await self.run_import()
        self.assertTrue(second["imported"])
        self.assertEqual(second["checks"]["wars"]["created"], 1)
        advanced = (await self.rows("SELECT * FROM wars WHERE id=?", (created["id"],)))[0]
        self.assertEqual((advanced["state"], advanced["last_seen_at"]), ("warEnded", (END_S + 4 * 86400 + 600) * SECOND))
        alias = (await self.rows("SELECT capture_state FROM war_legacy_ids WHERE legacy_id=?", (later,)))[0]
        self.assertEqual(alias["capture_state"], "warEnded")
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 3)
        self.assertEqual(len(await self.rows("SELECT * FROM imports")), 1)
        # The lineup row already there is not rewritten by the later capture.
        self.assertEqual((await self.rows("SELECT attacks_used FROM war_members WHERE war_id=?", (created["id"],)))[0]["attacks_used"], 0)
        third = await self.run_import()
        self.assertFalse(third["imported"])


if __name__ == "__main__":
    unittest.main()
