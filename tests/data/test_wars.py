"""Offline API-shaped war/CWL fixtures and persistent polling evidence."""

import asyncio
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.collectors.wars import due_jobs, path_for
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.schema import MIGRATIONS
from elbow_helper.data.store.war_projection import api_time
from elbow_helper.data.store.wars import SECOND, record_response
from elbow_helper.infrastructure.persistence.migrations import run_sqlite_migrations

from .test_family_store import CLANS, response
from .war_size import annual_size


END = api_time("20261028T180000.000Z")
START = END - 86400 * SECOND


def war(state="inWar", *, clans=("#PY", "#GR"), friendly=None, end="20261028T180000.000Z"):
    payload = {"state": state, "teamSize": 2, "attacksPerMember": 2,
               "preparationStartTime": "20261026T180000.000Z", "startTime": "20261027T180000.000Z",
               "endTime": end, "battleModifier": "none"}
    for index, key in enumerate(("clan", "opponent")):
        tags = ("#P0", "#P2") if index == 0 else ("#Q0", "#Q2")
        other = ("#Q0", "#Q2") if index == 0 else ("#P0", "#P2")
        members = []
        count = 0 if state == "preparation" else 2 if state == "warEnded" else 1
        for position, tag in enumerate(tags, 1):
            row = {"tag": tag, "name": f"Player {tag}", "townhallLevel": 16,
                   "mapPosition": position, "opponentAttacks": int(position <= count)}
            if position <= count:
                row["attacks"] = [{"attackerTag": tag, "defenderTag": other[position - 1],
                                   "stars": 3, "destructionPercentage": 100.0,
                                   "order": (position - 1) * 2 + index + 1, "duration": 123}]
                row["bestOpponentAttack"] = {"attackerTag": other[position - 1], "defenderTag": tag,
                                             "stars": 3, "destructionPercentage": 100.0,
                                             "order": (position - 1) * 2 + (1 - index) + 1, "duration": 123}
            members.append(row)
        payload[key] = {"tag": clans[index], "name": f"Clan {clans[index]}", "clanLevel": 20,
                        "badgeUrls": {"small": "https://example.test/badge"}, "attacks": count,
                        "stars": count * 3, "destructionPercentage": count * 50.0, "members": members}
    if friendly is not None:
        payload["isFriendly"] = friendly
    return payload


def war_log(payload=None, *, after=None):
    original = war("warEnded") if payload is None else payload
    entry = {key: deepcopy(original[key]) for key in ("endTime", "teamSize", "attacksPerMember", "clan", "opponent")}
    for key in ("clan", "opponent"):
        entry[key].pop("members")
    entry["result"] = "tie"
    if "isFriendly" in original:
        entry["isFriendly"] = original["isFriendly"]
    return {"items": [entry], "paging": {"cursors": {"after": after} if after else {}}}


def group(*, rounds=None, state="inWar", season="2026-10"):
    return {"state": state, "season": season,
            "clans": [{"tag": tag, "name": f"Clan {tag}", "clanLevel": 20,
                       "badgeUrls": {"small": "https://example.test/badge"},
                       "members": [{"tag": player_tag, "name": "Eligible", "townHallLevel": 16}]}
                      for tag, player_tag in (("#PY", "#P0"), ("#LQ", "#P2"), ("#GR", "#Q0"), ("#CU", "#Q2"))],
            "rounds": rounds if rounds is not None else [{"warTags": ["#R0", "#R2"]}, {"warTags": ["#0"]}]}


class WarFixture:
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "family.sqlite3"
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        self.at = START + 600 * SECOND

    async def rows(self, sql, args=()):
        return await self.store.call(lambda c: [dict(row) for row in c.execute(sql, args)])

    async def execute(self, sql, args=()):
        def write(c):
            c.execute(sql, args)
            c.commit()
        await self.store.call(write)

    async def record(self, payload, *, kind="current_war", scope="#PY", status=200, at=None):
        at = self.at if at is None else at
        await self.store.call(due_jobs, CLANS, at)
        if kind == "cwl_war":
            await self.execute(
                "INSERT OR IGNORE INTO war_collection_state(source_kind,scope_id,next_due_at) VALUES ('cwl_war',?,?)", (scope, at))
        run = await self.store.begin_run("wars", "family_wars_and_leagues", [(kind, scope)], at)
        await self.store.requested(run, 0)
        await self.store.call(record_response, run, 0, kind, scope, response(payload, status) if status is not None else None,
                              {c.tag for c in CLANS}, at)
        return await self.store.finish(run)


class WarStoreTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def test_both_sides_change_only_unique_attacks_and_final_capture_once(self):
        await self.record(war("preparation"))
        self.assertEqual(len(await self.rows("SELECT * FROM war_members")), 4)
        self.assertEqual(await self.rows("SELECT * FROM accounts"), [])
        await self.record(war())
        first = await self.rows("SELECT * FROM wars")
        attacks = await self.rows("SELECT * FROM war_attacks ORDER BY attack_order")
        await self.record(war(), at=self.at + 300 * SECOND)
        self.assertEqual(first, await self.rows("SELECT * FROM wars"))
        self.assertEqual(attacks, await self.rows("SELECT * FROM war_attacks ORDER BY attack_order"))
        self.assertEqual((await self.rows("SELECT rows_written FROM collection_runs ORDER BY id DESC LIMIT 1"))[0]["rows_written"], 0)
        report = await self.record(war("warEnded"), at=END + 90 * SECOND)
        self.assertEqual(report["status"], "ok")
        final = (await self.rows("SELECT * FROM wars"))[0]
        self.assertEqual((final["final_at"], final["roster_complete"], final["attacks_complete"]), (END + 90 * SECOND, 1, 1))
        self.assertEqual(len(await self.rows("SELECT * FROM war_attacks")), 4)
        await self.record(war("warEnded"), at=END + 600 * SECOND)
        self.assertEqual(final, (await self.rows("SELECT * FROM wars"))[0])

    async def test_restart_mid_war_and_failed_fetch_preserve_last_good(self):
        await self.record(war())
        before = await self.rows("SELECT * FROM wars")
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        for status in (None, 403, 404, 429, 500):
            report = await self.record(None, status=status)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(before, await self.rows("SELECT * FROM wars"))
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM war_attacks")), 4)

    async def test_family_mirrors_share_sorted_sides_and_global_attack_order(self):
        payload = war(clans=("#PY", "#LQ"))
        await self.record(payload)
        reverse = {**payload, "clan": payload["opponent"], "opponent": payload["clan"]}
        await self.record(reverse, scope="#LQ")
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 1)
        self.assertEqual([r["clan_tag"] for r in await self.rows("SELECT * FROM war_sides ORDER BY side")], ["#LQ", "#PY"])
        self.assertEqual(len(await self.rows("SELECT * FROM war_attacks")), 2)

    async def test_types_use_only_explicit_api_evidence(self):
        for friendly, expected in ((None, "UNKNOWN"), (True, "FRIENDLY"), (False, "REG")):
            with self.subTest(friendly=friendly):
                payload = war(friendly=friendly)
                payload["preparationStartTime"] = f"202610{20 + (friendly is True) + 2 * (friendly is False):02d}T180000.000Z"
                payload["endTime"] = f"202610{28 + (friendly is True) + 2 * (friendly is False):02d}T180000.000Z"
                await self.record(payload)
                self.assertEqual((await self.rows("SELECT type FROM wars ORDER BY id DESC LIMIT 1"))[0]["type"], expected)
        await self.record(war(), kind="cwl_war", scope="#R0")
        self.assertEqual((await self.rows("SELECT type,war_tag FROM wars WHERE war_tag IS NOT NULL"))[0], {"type": "CWL", "war_tag": "#R0"})

    async def test_missing_fields_and_colliding_orders_remain_repairable(self):
        partial = war("warEnded")
        partial["clan"]["members"][1]["attacks"][0]["order"] = 1
        report = await self.record(partial, at=END + 90 * SECOND)
        self.assertEqual(report["status"], "failed")
        row = (await self.rows("SELECT * FROM wars"))[0]
        self.assertEqual(row["attacks_complete"], 0)
        self.assertNotIn(1, [r["attack_order"] for r in await self.rows("SELECT * FROM war_attacks")])
        report = await self.record(war("warEnded"), at=END + 300 * SECOND)
        self.assertEqual(report["status"], "ok")
        self.assertEqual((await self.rows("SELECT attacks_complete FROM wars"))[0]["attacks_complete"], 1)

    async def test_missing_member_and_attack_fields_do_not_seal_an_ended_war(self):
        partial = war("warEnded")
        partial["opponent"].pop("members")
        await self.record(partial)
        self.assertEqual((await self.rows("SELECT roster_complete,attacks_complete FROM wars"))[0],
                         {"roster_complete": 0, "attacks_complete": 0})
        await self.record(war("warEnded"))
        self.assertEqual(len(await self.rows("SELECT * FROM war_attacks")), 4)

    async def test_best_opponent_attack_is_a_stub_until_full_outgoing_attack_arrives(self):
        payload = war()
        payload["opponent"]["members"][0].pop("attacks")
        await self.record(payload)
        stub = (await self.rows("SELECT * FROM war_attacks WHERE attack_order=2"))[0]
        self.assertTrue(json.loads(stub["extra_json"])["stub"])
        await self.record(war())
        full = (await self.rows("SELECT * FROM war_attacks WHERE attack_order=2"))[0]
        self.assertFalse(json.loads(full["extra_json"])["stub"])
        self.assertEqual((await self.rows("SELECT attacks_complete FROM wars"))[0]["attacks_complete"], 1)

    async def test_attack_identity_collision_across_polls_keeps_previous_attack(self):
        await self.record(war())
        before = (await self.rows("SELECT * FROM war_attacks WHERE attack_order=1"))[0]
        payload = war()
        payload["clan"]["members"][0]["attacks"][0]["defenderTag"] = "#Q2"
        payload["opponent"]["members"][0].pop("bestOpponentAttack")
        await self.record(payload)
        self.assertEqual(before, (await self.rows("SELECT * FROM war_attacks WHERE attack_order=1"))[0])
        self.assertEqual((await self.rows("SELECT attacks_complete FROM wars"))[0]["attacks_complete"], 0)

    async def test_war_log_recovers_aggregates_without_claiming_attacks_and_merges_both_orders(self):
        await self.record(war())
        await self.record(war_log(), kind="war_log", at=END + 300 * SECOND)
        row = (await self.rows("SELECT * FROM wars"))[0]
        self.assertEqual((row["state"], row["attacks_complete"], row["final_at"]), ("warEnded", 0, END + 300 * SECOND))
        # Existing live attacks are not proof of complete final detail.
        self.assertEqual(len(await self.rows("SELECT * FROM war_attacks")), 2)
        await self.record(war("warEnded"), at=END + 600 * SECOND)
        self.assertEqual(len(await self.rows("SELECT * FROM war_attacks")), 4)
        later = war("warEnded", clans=("#PY", "#CU"), end="20261030T180000.000Z")
        await self.record(war_log(later), kind="war_log")
        await self.record(later)
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 2)
        self.assertEqual(len(await self.rows("SELECT * FROM war_attacks")), 8)

    async def test_missing_private_and_malformed_war_logs_never_end_a_live_war(self):
        await self.record(war())
        before = await self.rows("SELECT * FROM wars")
        for payload, status in ((None, 403), (None, 404), ({}, 200), ({"items": [None]}, 200)):
            result = await self.record(payload, kind="war_log", status=status)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(before, await self.rows("SELECT * FROM wars"))
        result = await self.record({"items": [], "paging": {"cursors": {}}}, kind="war_log")
        self.assertEqual(result["status"], "empty")
        self.assertEqual(before, await self.rows("SELECT * FROM wars"))

    async def test_paged_log_commits_cursor_only_after_valid_page_and_encodes_it(self):
        await self.record(war_log(after="page+/="), kind="war_log")
        row = (await self.rows("SELECT * FROM war_collection_state WHERE source_kind='war_log' AND scope_id='#PY'"))[0]
        checkpoint = row["checkpoint_json"]
        self.assertEqual(path_for(row), "/clans/%23PY/warlog?limit=50&after=page%2B%2F%3D")
        await self.record({"items": [None]}, kind="war_log")
        self.assertEqual(checkpoint, (await self.rows("SELECT checkpoint_json FROM war_collection_state WHERE source_kind='war_log' AND scope_id='#PY'"))[0]["checkpoint_json"])
        await self.record({"items": [], "paging": {"cursors": {}}}, kind="war_log")
        finished = json.loads((await self.rows("SELECT checkpoint_json FROM war_collection_state WHERE source_kind='war_log' AND scope_id='#PY'"))[0]["checkpoint_json"])
        self.assertNotIn("after", finished)
        self.assertTrue(finished["history_complete"])

    async def test_cwl_rounds_discovered_once_all_tags_retained_unrelated_wars_not_polled(self):
        at = api_time("20261002T000000.000Z")
        await self.record(group(), kind="cwl_group", at=at)
        await self.record(group(), kind="cwl_group", scope="#LQ", at=at + 300 * SECOND)
        self.assertEqual(len(await self.rows("SELECT * FROM cwl_seasons")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM cwl_rounds")), 2)
        self.assertEqual(len(await self.rows("SELECT * FROM cwl_players")), 4)
        await self.record(war(clans=("#GR", "#CU")), kind="cwl_war", scope="#R2", at=at)
        self.assertEqual(await self.rows("SELECT * FROM wars"), [])
        self.assertEqual((await self.rows("SELECT enabled FROM war_collection_state WHERE scope_id='#R2'"))[0]["enabled"], 0)
        await self.record(war("warEnded"), kind="cwl_war", scope="#R0", at=END)
        self.assertEqual((await self.rows("SELECT enabled FROM war_collection_state WHERE scope_id='#R0'"))[0]["enabled"], 0)
        self.assertEqual(len(await self.rows("SELECT * FROM cwl_attacks")), 4)
        later = group(rounds=[{"warTags": ["#R0", "#R2"]}, {"warTags": ["#R8", "#R9"]}])
        await self.record(later, kind="cwl_group", at=END)
        self.assertEqual(len(await self.rows("SELECT * FROM cwl_rounds")), 4)
        self.assertEqual(len(await self.rows("SELECT * FROM war_collection_state WHERE source_kind='cwl_war'")), 4)
        await self.record(later, kind="cwl_group", at=END)
        self.assertEqual(len(await self.rows("SELECT * FROM war_collection_state WHERE source_kind='cwl_war' AND enabled=1")), 2)

    async def test_cwl_404_and_not_in_war_are_empty_failures_and_partial_groups_preserve_rounds(self):
        at = api_time("20261002T000000.000Z")
        await self.record(group(), kind="cwl_group", at=at)
        before = await self.rows("SELECT * FROM cwl_rounds")
        for payload, status, expected in ((None, 404, "empty"), ({"state": "notInWar"}, 200, "empty"),
                                          (None, 403, "failed"), (None, 429, "failed"),
                                          ({"state": "inWar", "season": "2026-10", "clans": [], "rounds": []}, 200, "failed")):
            result = await self.record(payload, kind="cwl_group", status=status, at=at)
            self.assertEqual(result["status"], expected)
            self.assertEqual(before, await self.rows("SELECT * FROM cwl_rounds"))

    async def test_cwl_pending_seasons_continue_after_day_eleven_until_final(self):
        at = api_time("20261002T000000.000Z")
        complete_group = group(rounds=[{"warTags": ["#R0", "#R2"]}, {"warTags": ["#R8", "#R9"]},
                                       {"warTags": ["#RC", "#RJ"]}], state="ended")
        await self.record(complete_group, kind="cwl_group", at=at)
        jobs = await self.store.call(due_jobs, CLANS, END)
        self.assertIn(("cwl_group", "#PY"), [(j["source_kind"], j["scope_id"]) for j in jobs])
        await self.record(war("warEnded"), kind="cwl_war", scope="#R0", at=END)
        await self.record(war("warEnded", clans=("#GR", "#CU")), kind="cwl_war", scope="#R2", at=END)
        for tag in ("#R8", "#R9", "#RC", "#RJ"):
            await self.record(war("warEnded", clans=("#GR", "#CU")), kind="cwl_war", scope=tag, at=END)
        self.assertIsNotNone((await self.rows("SELECT final_at FROM cwl_seasons"))[0]["final_at"])
        self.assertFalse(any(j["source_kind"] == "cwl_group" for j in await self.store.call(due_jobs, CLANS, END + 600 * SECOND)))

    async def test_persisted_end_deadline_retry_and_rollover_log_recovery(self):
        await self.record(war(), at=END - 120 * SECOND)
        due = (await self.rows("SELECT next_due_at FROM war_collection_state WHERE source_kind='current_war' AND scope_id='#PY'"))[0]["next_due_at"]
        self.assertEqual(due, END - 60 * SECOND)
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        await self.record(war(), at=END + 60 * SECOND)
        self.assertEqual((await self.rows("SELECT next_due_at FROM war_collection_state WHERE source_kind='current_war' AND scope_id='#PY'"))[0]["next_due_at"], END + 90 * SECOND)
        await self.record(war(), at=END + 90 * SECOND)
        self.assertEqual((await self.rows("SELECT next_due_at FROM war_collection_state WHERE source_kind='current_war' AND scope_id='#PY'"))[0]["next_due_at"], END + 270 * SECOND)
        await self.record({"state": "notInWar"}, at=END + 300 * SECOND)
        self.assertEqual((await self.rows("SELECT state FROM wars"))[0]["state"], "inWar")
        self.assertLessEqual((await self.rows("SELECT next_due_at FROM war_collection_state WHERE source_kind='war_log' AND scope_id='#PY'"))[0]["next_due_at"], END + 300 * SECOND)

    async def test_new_current_war_triggers_log_recovery_without_ending_previous(self):
        # The end timer's earlier log poll may have run before publication.
        await self.record(war(), at=END + 90 * SECOND)
        await self.execute("UPDATE war_collection_state SET next_due_at=? WHERE source_kind='war_log'", (END + 86400 * SECOND,))
        upcoming = war("preparation", end="20261030T180000.000Z")
        upcoming["preparationStartTime"] = "20261028T180000.000Z"
        upcoming["startTime"] = "20261029T180000.000Z"
        await self.record(upcoming, at=END + 300 * SECOND)
        self.assertEqual([r["state"] for r in await self.rows("SELECT state FROM wars ORDER BY id")], ["inWar", "preparation"])
        self.assertEqual((await self.rows("SELECT next_due_at FROM war_collection_state WHERE source_kind='war_log' AND scope_id='#PY'"))[0]["next_due_at"], END + 300 * SECOND)

    async def test_ambiguous_log_match_stays_separate_and_deduplicates(self):
        await self.record(war())
        other = war()
        other["preparationStartTime"] = "20261026T170000.000Z"
        await self.record(other)
        await self.record(war_log(), kind="war_log", at=END)
        await self.record(war_log(), kind="war_log", at=END + 300 * SECOND)
        rows = await self.rows("SELECT state,preparation_at,attacks_complete FROM wars ORDER BY id")
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["state"] for r in rows], ["inWar", "inWar", "warEnded"])
        self.assertIsNone(rows[-1]["preparation_at"])
        self.assertEqual(rows[-1]["attacks_complete"], 0)

    async def test_invalid_values_and_new_nested_identity_fields_are_not_persisted(self):
        await self.record(war())
        before = await self.rows("SELECT * FROM wars")
        for update in ({"state": []}, {"type": []}, {"teamSize": -1}, {"endTime": "not a time"}, {"battleModifier": {"tag": "#P0"}}):
            result = await self.record({**war(), **update})
            self.assertEqual(result["status"], "failed")
            self.assertEqual(before, await self.rows("SELECT * FROM wars"))
        partial = war("warEnded")
        partial["clan"]["members"][0]["newIdentity"] = {"name": "Unreviewed identity"}
        report = await self.record(partial)
        self.assertEqual(report["status"], "failed")
        self.assertNotIn("Unreviewed identity", str(await self.rows("SELECT * FROM war_members")))

    async def test_failed_final_fetch_retains_deadline_retry_and_cwl_incomplete_end_is_repaired(self):
        await self.record(war(), at=END + 60 * SECOND)
        await self.record(None, status=None, at=END + 90 * SECOND)
        self.assertEqual((await self.rows("SELECT next_due_at FROM war_collection_state WHERE source_kind='current_war' AND scope_id='#PY'"))[0]["next_due_at"], END + 270 * SECOND)
        partial = war("warEnded")
        partial["opponent"].pop("members")
        await self.record(partial, kind="cwl_war", scope="#R0")
        self.assertEqual((await self.rows("SELECT enabled FROM war_collection_state WHERE scope_id='#R0'"))[0]["enabled"], 1)
        await self.record(war("warEnded"), kind="cwl_war", scope="#R0")
        self.assertEqual((await self.rows("SELECT enabled FROM war_collection_state WHERE scope_id='#R0'"))[0]["enabled"], 0)

    async def test_log_frontier_gap_is_explicit_and_friendly_is_not_guessed(self):
        await self.record(war_log(war("warEnded", friendly=True)), kind="war_log")
        self.assertEqual((await self.rows("SELECT type FROM wars"))[0]["type"], "FRIENDLY")
        different = war("warEnded", clans=("#PY", "#CU"), friendly=False)
        await self.record(war_log(different), kind="war_log")
        checkpoint = json.loads((await self.rows("SELECT checkpoint_json FROM war_collection_state WHERE source_kind='war_log' AND scope_id='#PY'"))[0]["checkpoint_json"])
        self.assertTrue(checkpoint["api_history_limit"])
        self.assertFalse(checkpoint["history_complete"])
        self.assertEqual((await self.rows("SELECT type FROM wars ORDER BY id DESC LIMIT 1"))[0]["type"], "REG")

    async def test_existing_accounts_link_only_by_tag_opponents_never_enter_directory(self):
        await self.execute("INSERT INTO accounts(player_tag,first_observed_at,tracking_status) VALUES ('#P0',1,'observed')")
        await self.record(war())
        self.assertEqual(len(await self.rows("SELECT * FROM accounts")), 1)
        self.assertEqual([r["player_tag"] for r in await self.rows("SELECT * FROM war_members WHERE account_id IS NOT NULL")], ["#P0"])
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])

    async def test_sealed_unknown_type_can_gain_api_evidence_without_rewriting_attacks(self):
        await self.record(war("warEnded"), at=END)
        attacks = await self.rows("SELECT * FROM war_attacks ORDER BY attack_order")
        await self.record(war_log(war("warEnded", friendly=True)), kind="war_log", at=END + 300 * SECOND)
        row = (await self.rows("SELECT * FROM wars"))[0]
        self.assertEqual((row["type"], row["final_at"]), ("FRIENDLY", END))
        self.assertEqual(attacks, await self.rows("SELECT * FROM war_attacks ORDER BY attack_order"))

    async def test_failed_storage_projection_rolls_back_facts_and_reports_zero_rows(self):
        payload = war()
        payload["clan"]["members"][0]["townhallLevel"] = 2 ** 100
        result = await self.record(payload)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(await self.rows("SELECT * FROM wars"), [])
        self.assertEqual(await self.rows("SELECT * FROM war_sides"), [])
        self.assertEqual((await self.rows("SELECT rows_written FROM collection_runs"))[0]["rows_written"], 0)

    async def test_identical_poll_writes_only_the_run_row(self):
        await self.record(war())
        tables = [r["name"] for r in await self.rows("SELECT name FROM sqlite_master WHERE type='table'")]

        async def snapshot():
            rows = {table: await self.rows(f"SELECT * FROM {table}") for table in tables if table != "collection_runs"}
            # The schedule moves on; nothing else in the checkpoint may change.
            rows["war_collection_state"] = [{**row, "next_due_at": None} for row in rows["war_collection_state"]]
            return rows
        before = await snapshot()
        report = await self.record(war(), at=self.at + 300 * SECOND)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(before, await snapshot())
        self.assertEqual(before["collection_items"], [])
        runs = await self.rows("SELECT * FROM collection_runs ORDER BY id")
        self.assertEqual(len(runs), 2)
        self.assertEqual((runs[-1]["succeeded"], runs[-1]["rows_written"]), (1, 0))

    async def test_changed_facts_reference_their_run_and_only_failures_keep_receipts(self):
        await self.record(war("preparation"))
        await self.record(war())
        run = (await self.rows("SELECT MAX(id) AS id FROM collection_runs"))[0]["id"]
        self.assertEqual({r["source_run_id"] for r in await self.rows("SELECT source_run_id FROM war_attacks")}, {run})
        self.assertEqual((await self.rows("SELECT source_run_id FROM wars"))[0]["source_run_id"], run)
        self.assertEqual({r["source_run_id"] for r in await self.rows("SELECT source_run_id FROM war_members")}, {run - 1, run})
        await self.record({"state": "notInWar"}, scope="#LQ")
        self.assertEqual(await self.rows("SELECT * FROM collection_items"), [])
        report = await self.record(None, status=503)
        self.assertEqual((report["status"], report["failed"]), ("failed", 1))
        self.assertEqual(await self.rows("SELECT run_id,source_kind,scope_id,status,error_code,http_status FROM collection_items"),
                         [{"run_id": run + 2, "source_kind": "current_war", "scope_id": "#PY", "status": "failed",
                           "error_code": "fetch_failed", "http_status": 503}])
        partial = war("warEnded")
        partial["opponent"].pop("members")
        await self.record(partial, at=END + 90 * SECOND)
        self.assertEqual([r["error_code"] for r in await self.rows("SELECT error_code FROM collection_items ORDER BY id")],
                         ["fetch_failed", "partial_war"])

    async def test_restart_leaves_receipts_only_for_started_items_and_both_stay_due(self):
        await self.store.call(due_jobs, CLANS, self.at)
        run = await self.store.begin_run("wars", "family_wars_and_leagues",
                                         [("current_war", "#PY"), ("current_war", "#LQ")], self.at)
        await self.store.requested(run, 0)
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        row = (await self.rows("SELECT * FROM collection_runs"))[0]
        self.assertEqual((row["status"], row["failed"], row["skipped"]), ("interrupted", 1, 1))
        self.assertEqual([(r["ordinal"], r["scope_id"], r["error_code"]) for r in await self.rows("SELECT * FROM collection_items")],
                         [(0, "#PY", "interrupted")])
        self.assertEqual(await self.rows("SELECT * FROM war_collection_state WHERE active_run_id IS NOT NULL"), [])
        due = {(j["source_kind"], j["scope_id"]) for j in await self.store.call(due_jobs, CLANS, self.at)}
        self.assertLessEqual({("current_war", "#PY"), ("current_war", "#LQ")}, due)

    async def test_unqueued_response_is_refused_without_writing(self):
        await self.store.call(due_jobs, CLANS, self.at)
        run = await self.store.begin_run("wars", "family_wars_and_leagues", [("current_war", "#PY")], self.at)
        with self.assertRaisesRegex(ValueError, "war_source_not_queued"):
            await self.store.call(record_response, run, 0, "current_war", "#LQ", response(war()), {c.tag for c in CLANS}, self.at)
        self.assertEqual(await self.rows("SELECT * FROM wars"), [])

    async def test_routine_polls_share_one_slot_so_clans_share_a_run(self):
        await self.record(war())
        await self.record(war(clans=("#LQ", "#CU")), scope="#LQ", at=self.at + 7 * SECOND)
        self.assertEqual({r["next_due_at"] for r in await self.rows(
            "SELECT next_due_at FROM war_collection_state WHERE source_kind='current_war'")}, {self.at + 300 * SECOND})

    async def test_annual_storage_including_indexes_stays_a_few_hundred_megabytes(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.record(None, status=503, at=END + 300 * SECOND)
        await self.record(group(), kind="cwl_group", at=api_time("20261002T000000.000Z"))
        counts, annual_bytes = await self.store.call(annual_size)
        self.assertEqual(counts["wars"], 1642 + 756)
        self.assertEqual(counts["war_members"], 2398 * 60)
        self.assertEqual(counts["war_attacks"], 1642 * 120 + 756 * 60)
        self.assertEqual(counts["collection_runs"], 270320)
        self.assertEqual(counts["collection_items"], 30022)
        # WAL and backup copies are separate working space.
        self.assertLess(annual_bytes, 250_000_000)

    async def test_migrate_v3_preserves_existing_people_and_players(self):
        path = self.path.parent / "old.sqlite3"
        def old_database():
            with closing(sqlite3.connect(path)) as c:
                run_sqlite_migrations(c, MIGRATIONS[:3], target_version=3)
                c.execute("INSERT INTO accounts(player_tag,first_observed_at,tracking_status) VALUES ('#P0',1,'observed')")
                c.commit()
        await asyncio.to_thread(old_database)
        upgraded = FamilyStore(path)
        await upgraded.initialize()
        self.assertEqual(await upgraded.call(lambda c: c.execute("SELECT player_tag FROM accounts").fetchone()[0]), "#P0")
        self.assertEqual(await upgraded.call(lambda c: c.execute("PRAGMA foreign_key_check").fetchall()), [])


class WarCollectorTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = SimpleNamespace(get=AsyncMock())
        self.bot = SimpleNamespace(boot_complete=asyncio.Event())
        self.collector = FamilyCollectors(self.bot, self.store, self.client, CLANS)

    async def test_one_run_per_pass_failure_containment_and_restart_not_due(self):
        def fetch(path, **kwargs):
            if "warlog" in path:
                return response({"items": [], "paging": {"cursors": {}}})
            if "%23LQ" in path:
                raise TimeoutError("private transport detail")
            return response(war())
        self.client.get.side_effect = fetch
        with patch("elbow_helper.data.collectors.service.now_us", return_value=self.at), patch(
            "elbow_helper.data.collectors.wars.now_us", return_value=self.at), self.assertLogs(
                "elbow_helper.data.collectors.service", level="WARNING"):
            report = await self.collector.collect_wars()
        self.assertEqual(report["failed"], 1)
        self.assertEqual(len(await self.rows("SELECT * FROM collection_runs")), 1)
        self.assertEqual([r["scope_id"] for r in await self.rows("SELECT scope_id FROM collection_items")], ["#LQ"])
        self.assertEqual(self.client.get.await_count, 4)
        self.store = FamilyStore(self.path)
        self.collector.store = self.store
        with patch("elbow_helper.data.collectors.service.now_us", return_value=self.at + 30 * SECOND):
            await self.collector.collect_wars()
        self.assertEqual(self.client.get.await_count, 4)
        self.assertNotIn("private transport detail", str(await self.rows("SELECT * FROM collection_items")))

    async def test_group_discovery_fetches_unique_tags_same_pass_then_only_relevant_mutable(self):
        self.at = api_time("20261002T000000.000Z")
        def fetch(path, **kwargs):
            if "leaguegroup" in path:
                return response(group())
            if "clanwarleagues" in path:
                return response(war(clans=("#GR", "#CU") if path.endswith("R2") else ("#PY", "#GR")))
            if "warlog" in path:
                return response({"items": [], "paging": {"cursors": {}}})
            return response({"state": "notInWar"})
        self.client.get.side_effect = fetch
        with patch("elbow_helper.data.collectors.service.now_us", side_effect=lambda: self.at), patch(
            "elbow_helper.data.collectors.wars.now_us", side_effect=lambda: self.at):
            await self.collector.collect_wars()
            self.assertEqual(len(await self.rows("SELECT * FROM collection_runs")), 1)
            self.assertEqual(len([call for call in self.client.get.await_args_list if "clanwarleagues" in call.args[0]]), 2)
            self.at += 60 * SECOND
            self.client.get.reset_mock()
            await self.collector.collect_wars()
        self.assertEqual([call.args[0] for call in self.client.get.await_args_list], ["/clanwarleagues/wars/%23R0"])

    async def test_final_deadline_requests_fresh_and_disk_work_uses_worker(self):
        await self.record(war(), at=END + 60 * SECOND)
        # Only current-war final capture is due.
        await self.execute("UPDATE war_collection_state SET next_due_at=? WHERE source_kind!='current_war' OR scope_id!='#PY'", (END + 1000 * SECOND,))
        self.client.get.return_value = response(war("warEnded"))
        import elbow_helper.data.store as store_module
        original, threads = store_module.sqlite_connection, []
        def connect(*args, **kwargs):
            threads.append(threading.get_ident())
            return original(*args, **kwargs)
        with patch("elbow_helper.data.collectors.service.now_us", return_value=END + 90 * SECOND), patch(
            "elbow_helper.data.collectors.wars.now_us", return_value=END + 90 * SECOND), patch.object(
                store_module, "sqlite_connection", side_effect=connect):
            await self.collector.collect_wars()
        self.client.get.assert_awaited_once_with("/clans/%23PY/currentwar", fresh=True)
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)

    async def test_cancelled_war_fetch_records_interruption_and_shutdown_stops_work(self):
        entered = asyncio.Event()
        async def wait(path, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.client.get.side_effect = wait
        with patch("elbow_helper.data.collectors.service.now_us", return_value=self.at), patch(
            "elbow_helper.data.collectors.wars.now_us", return_value=self.at):
            task = asyncio.create_task(self.collector.collect_wars())
            await entered.wait()
            with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual((await self.rows("SELECT status FROM collection_runs"))[0]["status"], "interrupted")
        self.assertEqual([(r["status"], r["error_code"]) for r in await self.rows("SELECT * FROM collection_items")],
                         [("failed", "interrupted")])
        await self.collector.cog_unload()
        count = self.client.get.await_count
        await self.collector.collect_wars()
        self.assertEqual(self.client.get.await_count, count)
