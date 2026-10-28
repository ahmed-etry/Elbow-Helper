"""Sanitized real API shapes, strict failures and additive player upgrades."""

import asyncio
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.clans import collect_clans
from elbow_helper.data.store.people import mirror_links, observe_discord
from elbow_helper.data.store.players import collect_player
from elbow_helper.data.store.schema import MIGRATIONS, SCHEMA_VERSION
from elbow_helper.data.store.war_projection import api_time, project_log_entry, project_war
from elbow_helper.data.store.wars import SECOND, finish_seasons, record_response
from elbow_helper.infrastructure.persistence.migrations import run_sqlite_migrations

from .test_family_store import CLANS, clan_response, discord_member, link, player, response
from .test_players import profile
from .test_wars import WarFixture, group, war, war_log


FIXTURES = Path(__file__).parents[1] / "fixtures" / "family_api"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class WarFormatTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def record_sample(self, sample):
        kind, scope, body = sample["kind"], sample["scope"], sample["body"]
        await self.execute(
            "INSERT OR IGNORE INTO war_collection_state(source_kind,scope_id,next_due_at) VALUES (?,?,?)",
            (kind, scope, self.at))
        run = await self.store.begin_run("wars", "family_wars_and_leagues", [(kind, scope)], self.at)
        await self.store.requested(run, 0)
        family = {body["clan"]["tag"]} if kind == "cwl_war" else {scope}
        await self.store.call(record_response, run, 0, kind, scope, response(body), family, self.at)
        return await self.store.finish(run)

    async def test_all_real_shape_fixtures_store_without_failures_and_repeats_keep_provenance(self):
        for path in sorted(FIXTURES.glob("*.json")):
            if not path.stem.startswith(("current_war_", "cwl_group_", "cwl_war_", "war_log_")):
                continue
            with self.subTest(fixture=path.name):
                sample = fixture(path.name)
                report = await self.record_sample(sample)
                self.assertEqual(report["failed"], 0)
                if sample["kind"] in {"current_war", "cwl_war"} and sample["body"]["state"] != "notInWar":
                    projected = project_war(sample["body"], war_tag=sample["scope"] if sample["kind"] == "cwl_war" else None)
                    self.assertTrue(projected["roster_complete"])
                    self.assertTrue(projected["attacks_complete"])
                tables = ("wars", "war_sides", "war_members", "war_attacks", "cwl_log_summaries",
                          "cwl_clans", "cwl_players", "cwl_rounds", "cwl_seasons")
                before = {table: await self.rows(f"SELECT * FROM {table}") for table in tables}
                # A captured page may contain an after cursor. Repeat that same
                # request rather than submitting its cursor as the next page.
                if sample["kind"] == "war_log":
                    await self.execute("UPDATE war_collection_state SET checkpoint_json='{}' WHERE source_kind='war_log'")
                self.at += 300 * SECOND
                self.assertEqual((await self.record_sample(sample))["failed"], 0)
                self.assertEqual(before, {table: await self.rows(f"SELECT * FROM {table}") for table in tables})
        self.assertEqual(len(await self.rows("SELECT * FROM cwl_log_summaries")), 6)
        self.assertEqual(await self.rows("SELECT * FROM collection_items"), [])
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])

    async def test_full_date_season_is_month_identity_and_malformed_dates_keep_good_group(self):
        sample = fixture("cwl_group_1.json")
        self.assertEqual((await self.record_sample(sample))["failed"], 0)
        before = await self.rows("SELECT * FROM cwl_seasons")
        self.assertEqual(before[0]["season_key"], sample["body"]["season"][:7])
        for season in ("2026-02-30", "2026-99-01", "2026", None, "2026-1-001"):
            with self.subTest(season=season):
                bad = deepcopy(sample)
                bad["body"]["season"] = season
                self.assertEqual((await self.record_sample(bad))["failed"], 1)
                self.assertEqual(before, await self.rows("SELECT * FROM cwl_seasons"))
        sample["body"]["season"] = before[0]["season_key"]
        self.assertEqual((await self.record_sample(sample))["failed"], 0)
        self.assertEqual(len(await self.rows("SELECT * FROM cwl_seasons")), 1)

    async def test_cwl_squad_positions_are_preserved_and_invalid_positions_stay_partial(self):
        sample = fixture("cwl_war_1.json")
        body = sample["body"]
        self.assertTrue(any(m["mapPosition"] > body["teamSize"] for m in body["clan"]["members"]))
        for position in (0, 51, body["clan"]["members"][1]["mapPosition"]):
            with self.subTest(position=position):
                bad = deepcopy(sample)
                bad["body"]["clan"]["members"][0]["mapPosition"] = position
                self.assertEqual((await self.record_sample(bad))["failed"], 1)
                self.assertEqual((await self.rows("SELECT roster_complete FROM wars"))[0]["roster_complete"], 0)
        self.assertEqual((await self.record_sample(sample))["failed"], 0)
        stored = {r["player_tag"]: r["source_map_position"] for r in await self.rows("SELECT * FROM war_members")}
        self.assertEqual(stored, {m["tag"]: m["mapPosition"] for k in ("clan", "opponent") for m in body[k]["members"]})
        self.assertEqual((await self.rows("SELECT war_start_at FROM wars"))[0]["war_start_at"], api_time(body["warStartTime"]))
        regular = war()
        regular["clan"]["members"][0]["mapPosition"] = 3
        self.assertFalse(project_war(regular)["roster_complete"])

    async def test_malformed_war_start_time_does_not_replace_good_facts(self):
        sample = fixture("cwl_war_1.json")
        # Begin with a repairable war so invalid updates cannot hide behind sealing.
        sample["body"]["state"] = "inWar"
        await self.record_sample(sample)
        before = await self.rows("SELECT * FROM wars")
        for value in (None, "bad", "20000101T000000.000Z", "20990101T000000.000Z"):
            bad = deepcopy(sample)
            bad["body"]["warStartTime"] = value
            self.assertEqual((await self.record_sample(bad))["failed"], 1)
            self.assertEqual(before, await self.rows("SELECT * FROM wars"))

    async def test_defence_counter_does_not_override_complete_outgoing_records(self):
        payload = war("warEnded")
        payload["clan"]["members"][0]["opponentAttacks"] = 2
        self.assertEqual((await self.record(payload))["failed"], 0)
        row = (await self.rows("SELECT opponent_attacks FROM war_members WHERE player_tag='#P0'"))[0]
        self.assertEqual(row["opponent_attacks"], 2)
        self.assertEqual(len(await self.rows("SELECT * FROM war_attacks")), 4)

    async def test_missing_outgoing_record_and_global_order_gap_stay_partial(self):
        missing = war("warEnded")
        del missing["clan"]["members"][0]["attacks"]
        self.assertEqual((await self.record(missing))["failed"], 1)
        gap = war("warEnded")
        gap["opponent"]["members"][1]["attacks"][0]["order"] = 8
        gap["clan"]["members"][1]["bestOpponentAttack"]["order"] = 8
        self.assertEqual((await self.record(gap))["failed"], 1)
        self.assertEqual((await self.rows("SELECT attacks_complete FROM wars"))[0]["attacks_complete"], 0)

    async def test_weekly_log_totals_are_separate_and_bad_single_wars_are_still_refused(self):
        sample = fixture("war_log_1.json")
        summary = next(row for row in sample["body"]["items"] if "tag" not in row["opponent"])
        projected = project_log_entry(summary)
        self.assertTrue(projected["cwl_summary"])
        self.assertGreater(projected["destruction"], 100)
        smaller = deepcopy(summary)
        smaller["teamSize"] = 11
        smaller["clan"]["attacks"] = 77
        self.assertEqual(project_log_entry(smaller)["team_size"], 11)
        await self.record(war())
        for key, value in (("destructionPercentage", 100.1), ("destructionPercentage", float("nan")),
                           ("destructionPercentage", -1), ("attacks", -1)):
            bad = war_log()
            bad["items"][0]["clan"][key] = value
            before = await self.rows("SELECT * FROM wars")
            self.assertEqual((await self.record(bad, kind="war_log"))["failed"], 1)
            self.assertEqual(before, await self.rows("SELECT * FROM wars"))
        # A missing opponent tag alone must not authorize season-sized values.
        bad = war_log()
        del bad["items"][0]["opponent"]["tag"]
        self.assertEqual((await self.record(bad, kind="war_log"))["failed"], 1)
        for key, value in (("destructionPercentage", 700.1), ("destructionPercentage", float("inf")),
                           ("attacks", summary["teamSize"] * 7 + 1)):
            bad = deepcopy(summary)
            bad["clan"][key] = value
            with self.assertRaises(ValueError):
                project_log_entry(bad)
        # Validation is page-atomic: a later bad entry keeps both facts and cursor.
        await self.record_sample({**sample, "body": {"items": [summary], "paging": {"cursors": {}}}})
        before = await self.rows("SELECT * FROM cwl_log_summaries")
        bad = deepcopy(sample)
        bad["body"]["items"] = [summary, {"endTime": "bad"}]
        self.assertEqual((await self.record_sample(bad))["failed"], 1)
        self.assertEqual(before, await self.rows("SELECT * FROM cwl_log_summaries"))
        self.assertEqual(len(await self.rows("SELECT * FROM wars")), 1)

    async def test_missing_round_and_missing_war_stay_incomplete_until_repaired(self):
        complete = group(rounds=[{"warTags": ["#R0", "#R2"]}, {"warTags": ["#R8", "#R9"]},
                                 {"warTags": ["#RC", "#RJ"]}], state="ended")
        missing = deepcopy(complete)
        missing["rounds"].pop()
        await self.record(missing, kind="cwl_group")
        for tag in ("#R0", "#R2", "#R8", "#R9"):
            await self.record(war("warEnded", clans=("#GR", "#CU")), kind="cwl_war", scope=tag)
        self.assertIsNone((await self.rows("SELECT final_at FROM cwl_seasons"))[0]["final_at"])
        # A nonempty but short round also leaves a real gap.
        missing["rounds"].append({"warTags": ["#RC"]})
        await self.record(missing, kind="cwl_group")
        await self.record(war("warEnded"), kind="cwl_war", scope="#RC")
        self.assertIsNone((await self.rows("SELECT final_at FROM cwl_seasons"))[0]["final_at"])
        await self.record(complete, kind="cwl_group")
        # A missing checkpoint is not evidence that its round was collected.
        await self.execute("DELETE FROM war_collection_state WHERE source_kind='cwl_war' AND scope_id='#RJ'")
        await self.store.call(finish_seasons, self.at)
        self.assertIsNone((await self.rows("SELECT final_at FROM cwl_seasons"))[0]["final_at"])
        await self.record(war("warEnded", clans=("#GR", "#CU")), kind="cwl_war", scope="#RJ")
        self.assertIsNotNone((await self.rows("SELECT final_at FROM cwl_seasons"))[0]["final_at"])
        self.assertEqual(await self.rows("SELECT * FROM collection_items"), [])

    async def test_placeholder_and_empty_ended_rounds_do_not_finish_a_season(self):
        for rounds in ([], [{"warTags": ["#0"]}]):
            await self.record(group(rounds=rounds, state="ended"), kind="cwl_group")
            self.assertIsNone((await self.rows("SELECT final_at FROM cwl_seasons"))[0]["final_at"])

    async def test_player_fix_upgrade_preserves_every_existing_row_and_restarts(self):
        path = self.path.parent / "player_fix.sqlite3"

        def create_v4(connection):
            for migration in MIGRATIONS[:4]:
                migration.apply(connection)

        old = FamilyStore(path)
        with patch("elbow_helper.data.store.MIGRATIONS", MIGRATIONS[:4]), patch(
                "elbow_helper.data.store.SCHEMA_VERSION", 4), patch("elbow_helper.data.store.create_schema", create_v4):
            await old.initialize()
        # Populate people, membership and all four player fact tables using
        # today's collectors against the already-upgraded player schema.
        run = await old.begin_run("links", "current_links", [("account_links", "current")], 100)
        await old.call(mirror_links, run, link(), 100)
        await old.finish(run)
        run = await old.begin_run("discord", "guild", [("discord_scan", "1")], 200)
        await old.call(observe_discord, run, [discord_member()], 200, complete=True, scope="1")
        await old.finish(run)
        specs = [(kind, clan.tag) for clan in CLANS for kind in ("clan_profile", "clan_roster")]
        run = await old.begin_run("clans", "family_clans", specs, 300)
        await old.call(collect_clans, run, CLANS, [clan_response(0, [player()]), clan_response(1, [])], 300)
        await old.finish(run)
        run = await old.begin_run("players", "accounts", [("player_profile", "#P0")], 400)
        await old.requested(run, 0)
        account_id = await old.call(lambda c: c.execute("SELECT id FROM accounts WHERE player_tag='#P0'").fetchone()[0])
        payload = profile()
        duplicate = deepcopy(payload["achievements"][0])
        duplicate["info"] = "Different synthetic instructions"
        payload["achievements"].append(duplicate)
        await old.call(collect_player, run, 0, account_id, "#P0", response(payload), 400)
        self.assertEqual((await old.finish(run))["failed"], 0)

        def snapshot(c):
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}

        before = await old.call(snapshot)
        for table in ("player_observations", "player_items", "hero_equipment", "player_counters"):
            self.assertTrue(before[table], table)
        upgraded = FamilyStore(path)
        await upgraded.initialize()
        after = await upgraded.call(snapshot)
        self.assertEqual(before, {table: after[table] for table in before})
        self.assertEqual(await upgraded.call(lambda c: c.execute("PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
        await FamilyStore(path).initialize()
        self.assertEqual(after, await upgraded.call(snapshot))
        self.assertEqual(await upgraded.call(lambda c: c.execute("PRAGMA foreign_key_check").fetchall()), [])

    async def test_fresh_file_runs_player_fix_before_wars(self):
        def migrate():
            with closing(sqlite3.connect(":memory:")) as c:
                versions = run_sqlite_migrations(c, MIGRATIONS, target_version=SCHEMA_VERSION)
                return versions, {r[1] for r in c.execute("PRAGMA table_info(item_catalog)")}, c.execute(
                    "SELECT name FROM sqlite_master WHERE name='wars'").fetchone()[0]

        versions, columns, table = await asyncio.to_thread(migrate)
        self.assertEqual(versions, (1, 2, 3, 4, 5))
        self.assertIn("identity_key", columns)
        self.assertEqual(table, "wars")
