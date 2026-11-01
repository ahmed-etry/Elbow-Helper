"""The regular-war import: all-or-nothing, repeatable, alias-keeping and never over the record's own facts."""

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.reads import WarReads
from elbow_helper.data.reads.coverage import war_side_coverage
from elbow_helper.data.store import wars_import
from elbow_helper.data.store.legacy_links import ImportMismatch
from elbow_helper.data.store.wars_import import copy_at_startup, import_wars, snapshot_wars
from elbow_helper.data.store.war_projection import HOUR
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
        # A check that fails leaves nothing either.
        await self.execute("DELETE FROM war_legacy_ids")
        before = await self.dump()
        with patch.object(wars_import.Importer, "verify", side_effect=ImportMismatch("lineup_mismatch")):
            with self.assertRaisesRegex(ImportMismatch, "lineup_mismatch"):
                await self.run_import()
        self.assertEqual(await self.dump(), before)

    async def test_the_old_bot_writing_during_the_copy_cannot_break_it(self):
        await self.regular_war()
        later = old_id(END_S + 4 * 86400)
        real_write = wars_import.Importer.write

        def write_meanwhile(importer, events):
            # The old bot saves a new war and changes a lineup while the copy writes the record.
            with self.old() as old, old:
                old_war(old, later, end=END_S + 4 * 86400)
                old_lineup(old, later, [("#P0", 2)])
                old.execute("UPDATE war_roster_members SET attacks_used=2 WHERE war_id=?", (old_id(END_S),))
            return real_write(importer, events)
        with patch.object(wars_import.Importer, "write", write_meanwhile):
            result = await self.run_import()
        # The copy is of the snapshot it read: the new war and the changed lineup are not in it.
        self.assertTrue(result["imported"])
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 1)
        self.assertEqual([m["attacks_used"] for m in await self.rows("SELECT attacks_used FROM war_members")], [1, 1])
        self.assertEqual(len(await self.rows("SELECT * FROM imports WHERE topic='wars_regular'")), 1)

    async def test_one_read_transaction_takes_every_old_table_from_one_state(self):
        await self.regular_war()
        statements = []
        real_connect = sqlite3.connect

        def traced(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection
        with patch("elbow_helper.data.store.wars_import_plan.sqlite3.connect", traced):
            snapshot_wars(self.health)
        reads = [i for i, s in enumerate(statements) if s.startswith("SELECT war_id")]
        self.assertEqual(len(reads), 4)
        begin, end = statements.index("BEGIN"), statements.index("COMMIT")
        self.assertTrue(all(begin < i < end for i in reads))

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

    async def test_early_wars_with_final_participation_only_become_one_war_each(self):
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
            # Last captured hours before it ended, and an ID that cannot say its opponent.
            old_activity(old, unfinished, [("#P0", 1)], source=LIVE, end=other - 20 * 3600)
            old_attacks(old, unfinished, [(1, "#P0", "#Q0", 3)], end=other, state="inWar")
            old_activity(old, "REG:garbage", [("#P0", 1)], source=LIVE)
            old_attacks(old, "REG:orphan", [(1, "#P0", "#Q0", 3)])
        result = await self.run_import()
        checks = result["checks"]
        created = await self.rows("SELECT * FROM wars WHERE type_basis='legacy_unchecked'")
        self.assertEqual(len(created), 1)
        self.assertEqual((created[0]["type"], created[0]["preparation_at"], created[0]["team_size"], created[0]["state"],
                          created[0]["end_at"]), ("REG", None, None, "warEnded", (early + 7200) * SECOND))
        ours = await self.rows("SELECT war_id,legacy_id FROM war_legacy_ids")
        grouped = {}
        for row in ours:
            grouped.setdefault(row["war_id"], set()).add(row["legacy_id"])
        # The early war keeps both of its captures' IDs; the stored war keeps its late capture's.
        self.assertEqual(grouped[created[0]["id"]], {first, last})
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
        # Last checked hours before its end, so the copy cannot end it yet.
        checked = END_S + 4 * 86400 - 20 * 3600
        with self.old() as old, old:
            old_war(old, later, state="inWar", end=END_S + 4 * 86400, seen=checked)
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
        self.assertEqual((advanced["state"], advanced["last_seen_at"]), ("warEnded", (checked + 500) * SECOND))
        alias = (await self.rows("SELECT capture_state FROM war_legacy_ids WHERE legacy_id=?", (later,)))[0]
        self.assertEqual(alias["capture_state"], "warEnded")
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 3)
        self.assertEqual(len(await self.rows("SELECT * FROM imports")), 1)
        # The lineup row already there is not rewritten by the later capture.
        self.assertEqual((await self.rows("SELECT attacks_used FROM war_members WHERE war_id=?", (created["id"],)))[0]["attacks_used"], 0)
        third = await self.run_import()
        self.assertFalse(third["imported"])


PREP = "currentwar_api:preparation"


class CleaningTests(ImportFixture, unittest.IsolatedAsyncioTestCase):
    """Old wars the old bot never saved as ended, doubles and provisional end times."""

    async def missed(self, end_s):
        people = await WarReads(self.store).participation(["#PY"], (end_s - 3600) * SECOND, (end_s + 3600) * SECOND,
                                                          as_of=END + 40 * DAY)
        return {row["player_tag"]: row["missed"] for row in people["rows"]}

    async def war_ending(self, end_s):
        return (await self.rows("SELECT * FROM wars WHERE end_at=?", (end_s * SECOND,)))[0]

    async def test_a_war_left_in_battle_day_ends_at_its_end_when_last_checked_minutes_before(self):
        near, early, live = END_S, END_S - 5 * 86400, END_S + 40 * 86400
        with self.old() as old, old:
            old_war(old, old_id(near), state="inWar", seen=near - 600)
            old_lineup(old, old_id(near), [("#P0", 2), ("#P2", 1)], state="inWar", at=near - 600, source=LIVE)
            old_attacks(old, old_id(near), [(1, "#P0", "#Q0", 3), (2, "#P0", "#Q2", 2), (3, "#P2", "#Q0", 1)],
                        end=near - 600, state="inWar")
            # Battle-day rows carry their capture time where the end belongs.
            old.execute("UPDATE war_activity SET end_ts=? WHERE war_id=?", (near - 600, old_id(near)))
            # Last checked the day before its end; and a war still running when the copy runs.
            old_war(old, old_id(early, opponent="#CU"), opponent="#CU", state="inWar", end=early, seen=early - 20 * 3600)
            old_lineup(old, old_id(early, opponent="#CU"), [("#P0", 1), ("#P2", 0)], state="inWar", source=LIVE)
            old_war(old, old_id(live, opponent="#LQ"), opponent="#LQ", state="inWar", end=live, seen=live - 3600)
        result = await self.run_import()
        ended = await self.war_ending(near)
        self.assertEqual((ended["state"], ended["final_at"], ended["last_seen_at"]), ("warEnded", None, (near - 600) * SECOND))
        side = (await self.rows("SELECT attacks,extra_json FROM war_sides WHERE war_id=? AND clan_tag='#PY'", (ended["id"],)))[0]
        self.assertEqual((side["attacks"], json.loads(side["extra_json"])["derived"]), (3, ["attacks"]))
        # Its old ID still says what the old bot last saw.
        alias = (await self.rows("SELECT capture_state FROM war_legacy_ids WHERE war_id=?", (ended["id"],)))[0]
        self.assertEqual(alias["capture_state"], "inWar")
        self.assertEqual(await self.missed(near), {"#P0": 0, "#P2": 1})
        # The others keep their battle day: no one is counted as missing attacks there.
        self.assertEqual((await self.war_ending(early))["state"], "inWar")
        self.assertEqual(await self.missed(early), {"#P0": None, "#P2": None})
        self.assertEqual((await self.war_ending(live))["state"], "inWar")
        checks = result["checks"]
        self.assertEqual(checks["clans"]["ONE"]["stuck_wars"], {"ended_after_last_check": 1, "last_check_too_early": 1})
        self.assertEqual(checks["wars"]["ended_after_last_check"], {"warEnded": 1})
        self.assertEqual([(war["war_id"], war["created"], war["legacy"]) for war in checks["ended_after_last_check"]],
                         [(ended["id"], True, [old_id(near)])])

    async def test_a_war_left_in_preparation_is_ended_only_when_its_battle_day_was_seen(self):
        never, battled = END_S, END_S - 5 * 86400
        with self.old() as old, old:
            old_war(old, old_id(never), state="preparation", seen=never - 30 * 3600)
            old_lineup(old, old_id(never), [("#P0", 0), ("#P2", 0)], state="preparation", source=PREP)
            # An attack proves the battle day; the last check came minutes before the end.
            old_war(old, old_id(battled, opponent="#CU"), opponent="#CU", state="preparation", end=battled,
                    seen=battled - 300)
            old_lineup(old, old_id(battled, opponent="#CU"), [("#P0", 1), ("#P2", 0)], state="preparation", source=PREP)
            old_attacks(old, old_id(battled, opponent="#CU"), [(1, "#P0", "#Q0", 3)], end=battled, state="inWar")
        result = await self.run_import()
        self.assertEqual((await self.war_ending(never))["state"], "preparation")
        self.assertEqual(await self.missed(never), {"#P0": None, "#P2": None})
        self.assertEqual((await self.war_ending(battled))["state"], "warEnded")
        self.assertEqual(result["checks"]["clans"]["ONE"]["stuck_wars"], {
            "ended_after_last_check": 1, "preparation_battle_seen": 1, "preparation_never_in_battle": 1})

    async def test_a_war_known_only_from_battle_day_captures_ends_at_the_end_its_id_holds(self):
        end, other = END_S - 10 * 86400, END_S - 20 * 86400
        early, last = old_id(end - 3600, opponent="#CU"), old_id(end, opponent="#CU")
        lineup = [("#P0", 2), ("#P2", 1), ("#P8", 0), ("#PP", 0), ("#PL", 0)]
        with self.old() as old, old:
            # A preparation capture under an earlier ID, then battle-day captures under the ID the pause gave it.
            old_activity(old, early, [(player, 0) for player, _ in lineup], source=PREP, end=end - 30 * 3600)
            old_activity(old, last, lineup, source=LIVE, end=end - 480)
            old_attacks(old, last, [(1, "#P0", "#Q0", 3), (2, "#P0", "#Q2", 2), (3, "#P2", "#Q0", 1)],
                        end=end - 480, state="inWar")
            # Only ever captured in preparation: never a war, never an ID.
            old_activity(old, old_id(other, opponent="#LQ"), [("#P0", 0)], source=PREP, end=other - 30 * 3600)
        result = await self.run_import()
        war_row = await self.war_ending(end)
        self.assertEqual((war_row["state"], war_row["type_basis"], war_row["last_seen_at"]),
                         ("warEnded", "legacy_unchecked", (end - 480) * SECOND))
        members = await self.rows("SELECT player_tag,attacks_used FROM war_members WHERE war_id=? ORDER BY player_tag",
                                  (war_row["id"],))
        self.assertEqual([(m["player_tag"], m["attacks_used"]) for m in members], sorted(lineup))
        self.assertEqual(len(await self.rows("SELECT * FROM war_attacks WHERE war_id=?", (war_row["id"],))), 3)
        aliases = await self.rows("SELECT legacy_id,capture_state FROM war_legacy_ids ORDER BY legacy_id")
        self.assertEqual({(a["legacy_id"], a["capture_state"]) for a in aliases}, {(early, "preparation"), (last, "inWar")})
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 1)
        # The lineup's count is the team size, marked as the copy's own finding; misses need no town hall.
        self.assertEqual(war_row["team_size"], 5)
        self.assertNotIn("teamSize", json.loads(war_row["field_presence_json"]))
        side = (await self.rows("SELECT extra_json FROM war_sides WHERE war_id=? AND clan_tag='#PY'", (war_row["id"],)))[0]
        self.assertEqual(json.loads(side["extra_json"])["derived"], ["attacks", "team_size"])
        self.assertEqual(await self.missed(end), {"#P0": 0, "#P2": 1, "#P8": 2, "#PP": 2, "#PL": 2})
        clan = result["checks"]["clans"]["ONE"]
        self.assertEqual(clan["stuck_wars"], {"ended_after_last_check": 1})
        self.assertEqual(clan["activity_lineups"], {"team_size_from_lineup": 1})
        self.assertEqual(clan["preparation_only_ids"], {"battle_day_war": 1, "no_battle_day": 1})
        self.assertEqual(clan["excluded"]["war_activity"], {"no_battle_day": 1, "not_chosen_capture": 5})
        self.assertEqual(clan["merged_ids"], 1)
        # A member without their expected attacks takes the proof away.
        await self.execute("UPDATE war_members SET attacks_expected=NULL WHERE player_tag='#PL'")
        # Only an account that used every attack keeps its zero.
        self.assertEqual(await self.missed(end), {"#P0": 0, "#P2": None, "#P8": None, "#PP": None, "#PL": None})

    async def test_an_activity_lineup_that_is_not_a_whole_team_proves_no_misses(self):
        end = END_S - 10 * 86400
        with self.old() as old, old:
            old_activity(old, old_id(end, opponent="#CU"), [("#P0", 2), ("#P2", 1), ("#P8", 0), ("#PP", 0)], end=end)
            old_attacks(old, old_id(end, opponent="#CU"), [(1, "#P0", "#Q0", 3), (2, "#P0", "#Q2", 2),
                                                           (3, "#P2", "#Q0", 1)], end=end)
        result = await self.run_import()
        self.assertIsNone((await self.war_ending(end))["team_size"])
        self.assertEqual(await self.missed(end), {"#P0": 0, "#P2": None, "#P8": None, "#PP": None})
        self.assertEqual(result["checks"]["clans"]["ONE"]["activity_lineups"], {"lineup_incomplete": 1})

    async def test_misses_need_every_attack_the_war_reports(self):
        # A whole lineup, but one attack missing from the saved list: who made it can't be told.
        end = END_S - 10 * 86400
        lineup = [("#P0", 2), ("#P2", 1), ("#P8", 0), ("#PP", 0), ("#PL", 0)]
        with self.old() as old, old:
            old_activity(old, old_id(end, opponent="#CU"), lineup, end=end)
            old_attacks(old, old_id(end, opponent="#CU"), [(1, "#P0", "#Q0", 3), (3, "#P2", "#Q0", 1)], end=end)
        await self.run_import()
        war_id = (await self.war_ending(end))["id"]
        self.assertEqual(await self.missed(end), {"#P0": 0, "#P2": None, "#P8": None, "#PP": None, "#PL": None})
        coverage = await self.store.call(war_side_coverage, war_id, "#PY")
        self.assertEqual((coverage["own"]["attacks_reported"], coverage["own"]["attacks_stored"]), (3, 2))

    async def test_doubled_old_ids_of_one_war_are_merged_with_their_rows_counted_once(self):
        first, second = old_id(END_S), old_id(END_S + 600)
        with self.old() as old, old:
            for war_id, end in ((first, END_S), (second, END_S + 600)):
                old_war(old, war_id, end=end)
                old.execute("UPDATE wars SET preparation_start_ts=?,start_ts=? WHERE war_id=?",
                            (END_S - 2 * 86400, END_S - 86400, war_id))
                old_lineup(old, war_id, [("#P0", 1), ("#P2", 1)])
                old_attacks(old, war_id, [(1, "#P0", "#Q0", 3), (2, "#P2", "#Q2", 2)])
        result = await self.run_import()
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM war_legacy_ids")), 2)
        self.assertEqual(result["checks"]["inserted"], {"lineup_rows": 2, "attacks": 2})
        clan = result["checks"]["clans"]["ONE"]
        # Two lineup entries (each a lineup and an activity row) and two attacks under the second ID.
        self.assertEqual((clan["unique_wars"], clan["merged_ids"], clan["duplicate_rows"]), (1, 1, 6))


class StartupTests(ImportFixture, unittest.IsolatedAsyncioTestCase):
    """The copy runs once at startup, before the first war pass, and a failure changes nothing."""

    def collectors(self):
        bot = SimpleNamespace(boot_complete=asyncio.Event(), guild_id=1, get_guild=lambda guild_id: None)
        collectors = FamilyCollectors(bot, self.store, SimpleNamespace(get=AsyncMock()), (), old_wars=(self.health, None))
        return bot, collectors

    def startup(self, gate=None, order=None):
        def copy(connection, health, cache):
            if gate is not None:
                gate.wait(10)
            if order is not None:
                order.append("copy")
            return copy_at_startup(connection, health, cache, clans=CLANS)
        return patch("elbow_helper.data.collectors.service.copy_at_startup", copy)

    async def test_a_later_start_reads_nothing_and_adds_nothing(self):
        await self.regular_war()
        _, collectors = self.collectors()
        with self.startup(), self.assertLogs("elbow_helper.data.collectors.service", level="INFO") as logs:
            await collectors._copy_old_wars()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("Old wars copied", logs.output[0])
        settled = await self.dump()
        with self.startup(), patch.object(wars_import, "snapshot_wars", side_effect=AssertionError("read")), \
                self.assertNoLogs("elbow_helper.data.collectors.service", level="INFO"):
            await collectors._copy_old_wars()
        self.assertEqual(await self.dump(), settled)
        # The old files changing later does not bring the copy back.
        with self.old() as old, old:
            old.execute("UPDATE war_roster_members SET attacks_used=2")
        self.assertIsNone(await self.store.call(copy_at_startup, self.health, None, clans=CLANS))
        self.assertEqual(await self.dump(), settled)

    async def test_a_failed_copy_logs_one_line_changes_nothing_and_runs_again_at_the_next_start(self):
        await self.regular_war()
        with self.old() as old, old:
            old.execute("UPDATE war_attacks SET stars=7")
        before = await self.dump()
        _, collectors = self.collectors()
        with self.startup(), self.assertLogs("elbow_helper.data.collectors.service", level="INFO") as logs:
            await collectors._copy_old_wars()
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertIn("ImportMismatch: bad_row:war_attacks", logs.output[0])
        self.assertNotIn("\n", logs.output[0])
        self.assertEqual(await self.dump(), before)
        with self.old() as old, old:
            old.execute("UPDATE war_attacks SET stars=3")
        with self.startup():
            await collectors._copy_old_wars()
        self.assertEqual(len(await self.rows("SELECT * FROM imports WHERE topic='wars_regular'")), 1)

    async def test_the_first_war_pass_waits_for_the_copy_even_when_it_fails(self):
        for broken in (False, True):
            with self.subTest(broken=broken):
                await self.execute("DELETE FROM imports")
                with self.old() as old, old:
                    old.executescript("DELETE FROM wars; DELETE FROM war_roster_members; DELETE FROM war_attacks; "
                                      "DELETE FROM war_activity;")
                    old_war(old, old_id(END_S))
                    old_lineup(old, old_id(END_S), [("#P0", 1)])
                    old.execute("UPDATE wars SET state='over'" if broken else "SELECT 1")
                bot, collectors = self.collectors()
                order, gate = [], threading.Event()
                passes = AsyncMock(side_effect=lambda: order.append("war pass"))
                collectors.collect_wars = passes
                for name in ("collect_clans", "collect_players", "scan_discord"):
                    setattr(collectors, name, AsyncMock())
                with self.startup(gate, order), patch("elbow_helper.data.collectors.service.collect_sources", AsyncMock()), \
                        patch("elbow_helper.data.collectors.service.due_jobs", return_value=[{"due": True}]), \
                        self.assertLogs("elbow_helper.data.collectors.service", level="INFO"):
                    await collectors.cog_load()
                    bot.boot_complete.set()
                    await asyncio.sleep(0.3)
                    passes.assert_not_awaited()
                    gate.set()
                    for _ in range(100):
                        if passes.await_count:
                            break
                        await asyncio.sleep(0.05)
                    await collectors.cog_unload()
                self.assertEqual(order, ["copy", "war pass"])
                marker = await self.rows("SELECT * FROM imports WHERE topic='wars_regular'")
                self.assertEqual(len(marker), 0 if broken else 1)


if __name__ == "__main__":
    unittest.main()
