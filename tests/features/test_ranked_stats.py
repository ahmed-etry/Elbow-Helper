from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.ranked_stats.cog import RankedStats
from elbow_helper.features.ranked_stats.queries import RankedQueries
from elbow_helper.features.ranked_stats.store import RankedStore
from elbow_helper.features.ranked_stats.battles import log_identities, parse_log
from elbow_helper.features.ranked_stats.fields import project_player


def stamp(day=1, minute=0):
    return (datetime(2026, 9, day, tzinfo=timezone.utc) + timedelta(minutes=minute)).isoformat(timespec="microseconds")


def observation(tag="#P0", *, wins=10, day=1, minute=0, trophies=100,
                member=42, clan="#Q0", status="ok", season=None):
    fields = {"trophies": trophies}
    if wins is not None:
        fields["attackWins"] = wins
    if season:
        fields["legendStatistics"] = {"currentSeason": {"id": season}}
    return {"player_tag": tag, "observed_at": stamp(day, minute), "member_id": member,
            "clan_tag": clan, "player_name": "Synthetic account", "status": status, "fields": fields}


def battle(*, day=1, minute=1, mode="RANKED", attack=True, opponent="#Q2", stars=0):
    return {"battleType": mode, "attack": attack, "battleTimestamp": stamp(day, minute),
            "opponentPlayerTag": opponent, "stars": stars, "destructionPercentage": 10,
            "opponentName": "Synthetic opponent"}


def save_log(store, items, row=None):
    row = row or observation()
    poll, records = parse_log({"items": items}, row, lambda *_: None)
    store.append([row], battles=records, polls=[poll])
    return poll, records


class RankedWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ranked.sqlite3"
        self.store = RankedStore(self.path)
        self.store.initialize()
        self.queries = RankedQueries(self.store)

    def read(self, selector="member_ids", values=(42,), **arguments):
        return self.queries.window(after=stamp(), before=stamp(4), selector=selector, values=values, **arguments)

    def test_existing_snapshot_database_is_migrated_without_inferring_attempts(self):
        self.store.append([observation(wins=10), observation(wins=20, minute=5)])
        import sqlite3
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("DROP TABLE ranked_battles")
            conn.execute("DROP TABLE battle_log_polls")
            conn.commit()
        self.store.initialize()
        row = self.read()["accounts"][0]
        self.assertEqual(row["sample_count"], 2)
        self.assertEqual(row["ranking"]["attackWins"], 20)
        self.assertIsNone(row["attacks_used"])
        self.assertEqual(row["daily"], [])
        self.assertIsNone(row["attacks_available"])

    def test_opponent_nullable_migration_preserves_existing_rows_and_indexes(self):
        import sqlite3
        save_log(self.store, [battle()])
        with closing(sqlite3.connect(self.path)) as conn:
            original = conn.execute("SELECT * FROM ranked_battles").fetchall()
            conn.execute("ALTER TABLE ranked_battles RENAME TO saved_battles")
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='saved_battles'").fetchone()[0]
            conn.execute(schema.replace('"saved_battles"', 'ranked_battles').replace(
                "opponent_tag TEXT", "opponent_tag TEXT NOT NULL"))
            conn.execute("INSERT INTO ranked_battles SELECT * FROM saved_battles")
            conn.execute("DROP TABLE saved_battles")
            conn.commit()
        self.store.initialize()
        self.store.initialize()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT * FROM ranked_battles").fetchall(), original)
            columns = conn.execute("PRAGMA table_info(ranked_battles)").fetchall()
            self.assertEqual(next(row[3] for row in columns if row[1] == "opponent_tag"), 0)
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(ranked_battles)")}
            self.assertTrue({"battles_time", "battles_member", "battles_clan"} <= indexes)
        save_log(self.store, [battle(minute=2, opponent=None)])
        save_log(self.store, [dict(battle(minute=2, opponent=None), stars=3)], observation(minute=5))
        self.assertEqual(self.read()["accounts"][0]["battle_records"], 2)

    def test_ranked_losses_legend_attacks_and_defenses_have_distinct_counts(self):
        save_log(self.store, [battle(stars=0), battle(minute=2, stars=3),
            battle(minute=3, mode="LEGEND"), battle(minute=4, attack=False),
            battle(minute=5, mode="HOME_VILLAGE")])
        row = self.read()["accounts"][0]
        self.assertEqual(row["attacks_used"], 3)
        self.assertEqual(row["battle_records"], 4)
        self.assertEqual(row["daily"], [{"date": "2026-09-01", "attacks_used": 3, "defenses": 1}])
        self.assertFalse(row["counts_complete"])
        self.assertIsNone(row["attacks_available"])

    def test_overlapping_logs_restarts_and_corrected_results_do_not_duplicate_battles(self):
        entry = battle()
        save_log(self.store, [entry, entry])
        # Results/names are mutable; identities and first-observed ownership are stable.
        changed = {**entry, "stars": 3, "opponentName": "Updated opponent"}
        restarted = RankedStore(self.path)
        restarted.initialize()
        save_log(restarted, [changed, battle(minute=2)], observation(minute=5, member=43))
        result = RankedQueries(restarted).window(after=stamp(), before=stamp(4), selector="account_tags", values=("#P0",))
        self.assertEqual(result["accounts"][0]["attacks_used"], 2)
        records, _, _ = restarted.read_battle_window(after=stamp(), before=stamp(4), selector="member_ids", values=(42,))
        self.assertEqual(len(records), 1)
        import json
        self.assertEqual(json.loads(records[0]["payload_json"])["stars"], 3)

    def test_battle_time_controls_day_and_window_even_when_backfilled_later(self):
        save_log(self.store, [battle(minute=1439), battle(day=2, minute=0), battle(day=4, minute=0)],
                 observation(day=5))
        row = self.read()["accounts"][0]
        self.assertEqual(row["attacks_used"], 2)
        self.assertEqual([day["date"] for day in row["daily"]], ["2026-09-01", "2026-09-02"])
        self.assertEqual(row["sample_count"], 0)
        self.assertIsNone(row["ranking_observed_at"])

    def test_first_observed_associations_are_used_for_member_and_clan_filters(self):
        save_log(self.store, [battle()], observation(member=42, clan="#Q0"))
        save_log(self.store, [battle(), battle(minute=2)], observation(minute=5, member=43, clan="#Q2"))
        for selector, value in (("member_ids", 42), ("clan_tags", "#Q0")):
            with self.subTest(selector=selector):
                self.assertEqual(self.read(selector, (value,))["accounts"][0]["attacks_used"], 1)

    def test_ranks_ties_paging_unobserved_accounts_and_half_open_window(self):
        self.store.append([observation("#P0", trophies=200), observation("#P2", trophies=200),
                           observation("#P8", trophies=100), observation("#P9", trophies=900, day=4)])
        first = self.read("account_tags", ("#P0", "#P2", "#P8", "#P9"), limit=2)
        self.assertEqual([row["selection_rank"] for row in first["accounts"]], [1, 1])
        self.assertEqual(first["next_offset"], 2)
        self.assertEqual(first["unobserved_selections"], ["#P9"])
        self.assertEqual(self.read(limit=2, offset=2)["accounts"][0]["selection_rank"], 3)

    def test_failed_profile_snapshots_keep_stale_ranking(self):
        self.store.append([observation(), observation(minute=5, status="unavailable")])
        row = self.read()["accounts"][0]
        self.assertEqual(row["failed_samples"], 1)
        self.assertEqual(row["status"], "stale")
        self.assertIsNone(row["attacks_used"])

    def test_row_limit_can_stop_before_the_selected_ownership_observation(self):
        import json
        row = observation(member=43)
        row["fields_json"] = json.dumps(row.pop("fields"))
        with patch.object(self.store, "read_window", return_value=([row], True)):
            result = self.read()
        self.assertEqual(result["accounts"], [])
        self.assertFalse(result["complete"])
        self.assertIn("snapshot_row_limit_reached", result["limitations"])


class BattleParsingTests(unittest.TestCase):
    def test_missing_fields_unknown_modes_and_bad_dates_do_not_drop_valid_records(self):
        bad = [dict(battle(), battleTimestamp=None), dict(battle(), attack=1),
               dict(battle(), opponentPlayerTag=None), dict(battle(), battleType={}),
               dict(battle(), battleTimestamp="2026-09-01T00:00:00"), None]
        missing = []
        poll, records = parse_log({"items": [battle(), *bad]}, observation(),
                                  lambda field, invalid: missing.append((field, invalid)))
        self.assertEqual(len(records), 2)
        self.assertEqual(poll["status"], "partial")
        self.assertEqual(poll["skipped_entries"], len(bad) - 1)
        self.assertIn(("battlelog.battleTimestamp", True), missing)

    def test_optional_opponent_identity_is_stable_and_quiet(self):
        for opponent in (None, "invalid", "#Q2"):
            for name in (None, "", "Synthetic opponent"):
                with self.subTest(opponent=opponent, name=name):
                    entry = dict(battle(opponent=opponent), opponentName=name)
                    if opponent is None:
                        entry.pop("opponentPlayerTag")
                    missing = []
                    poll, rows = parse_log([entry, entry], observation(),
                        lambda field, invalid: invalid and missing.append(field))
                    corrected = {**entry, "stars": 3, "army": []}
                    if opponent == "#Q2":
                        corrected["opponentName"] = "Renamed opponent"
                    _, later = parse_log([corrected], observation(minute=5), lambda *_: None)
                    self.assertEqual((poll["status"], poll["skipped_entries"], len(rows)), ("ok", 0, 1))
                    self.assertEqual(rows[0]["battle_key"], later[0]["battle_key"])
                    self.assertEqual(rows[0]["opponent_tag"], opponent if opponent == "#Q2" else None)
                    self.assertEqual(log_identities([entry]), log_identities([corrected]))
                    self.assertEqual(missing, [])

    def test_opponent_names_distinguish_tagless_battles(self):
        entries = [dict(battle(opponent=None), opponentName=name)
                   for name in ("Synthetic one", "Synthetic two", None)]
        _, rows = parse_log(entries, observation(), lambda *_: None)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(log_identities(entries)), 3)

    def test_battle_types_match_regardless_of_case_and_other_modes_are_not_skips(self):
        missing = []
        poll, records = parse_log([battle(mode="ranked"), battle(minute=2, mode="FRIENDLY_CHALLENGE")],
                                  observation(), lambda field, invalid: invalid and missing.append(field))
        self.assertEqual([record["battle_type"] for record in records], ["RANKED"])
        self.assertEqual((poll["status"], missing), ("ok", []))

    def test_api_compact_times_and_offsets_are_normalized_to_utc(self):
        items = [dict(battle(), battleTimestamp="20260901T235900.000Z"),
                 dict(battle(minute=2), battleTimestamp="2026-09-02T01:00:00+02:00")]
        _, records = parse_log(items, observation(), lambda *_: None)
        self.assertEqual([record["battle_at"][:10] for record in records], ["2026-09-01"] * 2)

    def test_poll_interval_uses_returned_log_count_and_all_modes_turnover(self):
        short, _ = parse_log([battle(minute=value * 10, mode="HOME_VILLAGE") for value in range(4)], observation(), lambda *_: None)
        long, _ = parse_log([battle(minute=value * 10, mode="HOME_VILLAGE") for value in range(8)], observation(), lambda *_: None)
        self.assertEqual(short["poll_interval_seconds"], 600)
        self.assertEqual(long["poll_interval_seconds"], 1200)
        self.assertEqual(short["total_entries"], 4)
        burst, _ = parse_log([battle(minute=0), battle(minute=0)], observation(), lambda *_: None)
        self.assertEqual(burst["poll_interval_seconds"], 300)
        slow, _ = parse_log([battle(minute=0), battle(minute=300)], observation(), lambda *_: None)
        self.assertEqual(slow["poll_interval_seconds"], 3600)
        for items in ([], [battle()], [dict(battle(), battleTimestamp=None)]):
            with self.subTest(items=items):
                poll, _ = parse_log(items, observation(), lambda *_: None)
                self.assertEqual(poll["poll_interval_seconds"], 300)


class PlayerFieldTests(unittest.TestCase):
    def test_sections_absent_outside_legend_league_are_not_reported(self):
        missing = []
        fields = project_player({"trophies": 100, "bestTrophies": 100, "attackWins": 0, "defenseWins": 0,
                                 "leagueTier": {"id": 1, "name": "Synthetic league"},
                                 "legendStatistics": {"bestSeason": {"trophies": 100}}},
                                lambda field, invalid: invalid and missing.append(field))
        self.assertEqual(missing, [])
        self.assertEqual(fields["legendStatistics"], {"bestSeason": {"trophies": 100}})

    def test_malformed_optional_sections_are_still_reported(self):
        missing = []
        project_player({"leagueTier": {"id": 1, "name": "Synthetic league"}, "league": "wrong",
                        "legendStatistics": {"currentSeason": {"rank": -1}}},
                       lambda field, invalid: invalid and missing.append(field))
        self.assertIn("league", missing)
        self.assertIn("legendStatistics.currentSeason.rank", missing)
        self.assertNotIn("legendStatistics.currentSeason.id", missing)


class RankedCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_battle_log_404_preserves_valid_profiles_and_reports_endpoint_once(self):
        def response(path, **kwargs):
            if path.endswith("/battlelog"):
                return SimpleNamespace(ok=False, status=404, payload=None)
            return self.response(path, **kwargs)

        self.client.get.side_effect = response
        with self.assertLogs("elbow_helper.features.ranked_stats.cog", level="WARNING") as logs:
            await self.collect_at(0)
            await self.collect_at(600)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("battle log unavailable for 2 accounts", logs.output[0])
        self.assertIn("status 404", logs.output[0])
        self.assertFalse(self.cog._health.missing_players)
        self.assertEqual(self.cog._schedules["#P0"].player_name, "Synthetic account")
        self.assertEqual(self.cog._schedules["#P0"].battle_interval, 1200)

    async def test_transport_timeouts_stay_debug_and_preserve_backoff(self):
        self.client.get.side_effect = TimeoutError("synthetic timeout")
        logger = "elbow_helper.features.ranked_stats.cog"
        with self.assertLogs(logger, level="DEBUG") as logs:
            await self.collect_at(0)
        self.assertTrue(all(record.levelname == "DEBUG" for record in logs.records))
        self.assertEqual(self.cog._schedules["#P0"].battle_interval, 600)
        with self.assertNoLogs(logger, level="WARNING"):
            await self.collect_at(600)
        self.assertEqual(self.cog._schedules["#P0"].battle_interval, 1200)
        with self.assertLogs(logger, level="WARNING") as logs:
            await self.collect_at(1800)
        # Accounts crossing the threshold together are summarised in one line.
        self.assertEqual(len(logs.output), 1)
        self.assertIn("2 accounts after 3+ consecutive polls", logs.output[0])
        self.assertEqual(self.cog._schedules["#P0"].battle_interval, 2400)
        self.client.get.side_effect = self.response
        with self.assertLogs(logger, level="INFO") as logs:
            await self.collect_at(4200)
        recovered = [line for line in logs.output if "recovered" in line]
        self.assertEqual(len(recovered), 1)
        self.assertIn("recovered for 2 accounts", recovered[0])

    async def test_invalid_link_warning_is_once_until_repaired(self):
        self.links["invalid"] = {"discord_user_id": 0}
        self.client.get.side_effect = self.response
        logger = "elbow_helper.features.ranked_stats.cog"
        with self.assertLogs(logger, level="WARNING") as logs:
            await self.collect_at(0)
            await self.collect_at(60)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("invalid linked account identities", logs.output[0])
        del self.links["invalid"]
        with self.assertLogs(logger, level="INFO") as logs:
            await self.collect_at(120)
            await self.collect_at(180)
        self.assertEqual(len(logs.output), 1)
        self.links["invalid"] = {"discord_user_id": 0}
        with self.assertLogs(logger, level="WARNING"):
            await self.collect_at(240)

    async def test_no_response_and_http_failure_stay_debug_for_single_poll(self):
        for status, expected in ((None, "(no response)"), (503, "(status 503)")):
            with self.subTest(status=status):
                self.client.get.side_effect = lambda *_args, **_kwargs: SimpleNamespace(
                    ok=False, status=status, payload=None)
                with self.assertLogs("elbow_helper.features.ranked_stats.cog", level="DEBUG") as logs:
                    await self.collect_at(0 if status is None else 7200)
                self.assertTrue(all(record.levelname == "DEBUG" for record in logs.records))
                self.assertTrue(any(expected in line for line in logs.output))

    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = SimpleNamespace(configured=True, get=AsyncMock())
        self.links = {"#P0": {"discord_user_id": 42}, "#P2": {"discord_user_id": 43}}
        links = SimpleNamespace(get_all_links=lambda: self.links)
        self.bot = SimpleNamespace(clash_client=self.client, paths=SimpleNamespace(data_root=Path(self.temp.name)))
        self.cog = RankedStats(self.bot, links)
        self.cog.store.initialize()
        self.seconds = 0
        clock = patch("elbow_helper.features.ranked_stats.cog.datetime")
        self.addCleanup(clock.stop)
        clock.start().now.side_effect = lambda *_: datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(seconds=self.seconds)
        timer = patch("elbow_helper.features.ranked_stats.cog.monotonic", side_effect=lambda: self.seconds)
        self.addCleanup(timer.stop)
        timer.start()
        real_sleep = asyncio.sleep

        async def sleep(delay):
            self.seconds += delay
            await real_sleep(0)

        sleeper = patch("elbow_helper.features.ranked_stats.cog.asyncio.sleep", side_effect=sleep)
        self.addCleanup(sleeper.stop)
        sleeper.start()

    def response(self, path, **kwargs):
        if path.endswith("/battlelog"):
            spacing = 10 if "P0/" in path else 40
            return SimpleNamespace(ok=True, status=200,
                payload={"items": [battle(minute=0), battle(minute=spacing)]})
        return SimpleNamespace(ok=True, status=200, payload={
            "tag": path.rsplit("/", 1)[-1].replace("%23", "#"), "name": "Synthetic account",
            "clan": {"tag": "#Q0"}, "trophies": 100, "bestTrophies": 100,
            "attackWins": 0, "defenseWins": 0, "league": {"id": 1, "name": "Synthetic league"},
            "leagueTier": {"id": 1, "name": "Synthetic league"},
            "legendStatistics": {"legendTrophies": 0, **{
                season: {"id": "sample", "rank": 1, "trophies": 100}
                for season in ("currentSeason", "previousSeason", "bestSeason")}}})

    async def collect_at(self, seconds):
        self.seconds = seconds
        self.client.get.reset_mock()
        await self.cog.collect_once()
        return [call.args[0] for call in self.client.get.await_args_list]

    def restart_collector(self):
        self.cog = RankedStats(self.bot, self.cog.account_links)
        self.cog.store.initialize()

    async def test_missing_profile_backs_off_both_endpoints_and_resumes_on_success(self):
        self.links = {"#P0": {"discord_user_id": 42}}
        status = 404

        def response(path, **kwargs):
            if status != 200:
                return SimpleNamespace(ok=False, status=500 if path.endswith("/battlelog") else status,
                                       payload=None)
            return self.response(path, **kwargs)

        self.client.get.side_effect = response
        logger = "elbow_helper.features.ranked_stats.cog"
        with self.assertLogs(logger, level="WARNING") as logs:
            self.assertEqual(await self.collect_at(0), ["/players/%23P0"])
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.cog._schedules["#P0"].profile_due, 86400)
        for seconds in (60, 3600, 86399):
            self.assertEqual(await self.collect_at(seconds), [])
        with self.assertLogs(logger, level="DEBUG") as logs:
            self.assertEqual(await self.collect_at(86400), ["/players/%23P0"])
        self.assertTrue(all(record.levelname == "DEBUG" for record in logs.records))
        status = 200
        with self.assertLogs(logger, level="INFO") as logs:
            self.assertEqual(await self.collect_at(172800), ["/players/%23P0", "/players/%23P0/battlelog"])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("recovered after status 404", logs.output[0])
        self.assertFalse(self.cog._schedules["#P0"].missing)
        self.assertEqual(self.cog._schedules["#P0"].profile_due, 176400)
        self.assertEqual(await self.collect_at(173100), ["/players/%23P0/battlelog"])

    async def test_missing_warning_cooldown_and_recovery_survive_restarts(self):
        self.links = {"#P0": {"discord_user_id": 42}}
        status = 404
        self.client.get.side_effect = lambda path, **kwargs: (
            self.response(path, **kwargs) if status == 200 else
            SimpleNamespace(ok=False, status=status, payload=None))
        logger = "elbow_helper.features.ranked_stats.cog"
        with self.assertLogs(logger, level="WARNING") as logs:
            await self.collect_at(0)
        self.assertEqual(len(logs.output), 1)
        self.restart_collector()
        with self.assertNoLogs(logger, level="INFO"):
            self.assertEqual(await self.collect_at(60), [])
        self.assertEqual(self.cog._schedules["#P0"].profile_due, 86400)
        with self.assertLogs(logger, level="DEBUG") as logs:
            self.assertEqual(await self.collect_at(86400), ["/players/%23P0"])
        self.assertTrue(all(record.levelname == "DEBUG" for record in logs.records))
        status = 503  # An unvalidated retry must preserve the missing-player state.
        with self.assertLogs(logger, level="DEBUG"):
            await self.collect_at(172800)
        self.restart_collector()
        self.assertEqual(await self.collect_at(172860), [])
        self.assertTrue(self.cog._schedules["#P0"].missing)
        self.assertEqual(self.cog._schedules["#P0"].profile_due, 259200)
        status = 200
        with self.assertLogs(logger, level="INFO") as logs:
            await self.collect_at(259200)
        self.assertEqual(len(logs.output), 1)
        self.restart_collector()
        with self.assertNoLogs(logger, level="INFO"):
            await self.collect_at(259260)
            # A later 404 for this same tag stays DEBUG, including after restart.
            status = 404
            self.assertEqual(await self.collect_at(262800), ["/players/%23P0"])
        self.assertTrue(self.cog._schedules["#P0"].missing)
        self.assertEqual(self.cog.store.profile_health({"#P0"}), {"#P0": True})

    async def test_link_changes_retry_missing_tags_and_collect_corrected_tags_immediately(self):
        self.links = {"#P0": {"discord_user_id": 42}}
        missing = True
        self.client.get.side_effect = lambda path, **kwargs: (
            SimpleNamespace(ok=False, status=404, payload=None)
            if path.endswith("P0") and missing else self.response(path, **kwargs))
        logger = "elbow_helper.features.ranked_stats.cog"
        with self.assertLogs(logger, level="WARNING"):
            await self.collect_at(0)
        self.links.clear()
        self.assertEqual(await self.collect_at(60), [])
        self.links["#P8"] = {"discord_user_id": 42}
        self.assertEqual(await self.collect_at(120), ["/players/%23P8", "/players/%23P8/battlelog"])
        self.links["#P0"] = {"discord_user_id": 42}
        with self.assertLogs(logger, level="DEBUG") as logs:
            self.assertEqual(await self.collect_at(180), ["/players/%23P0"])
        self.assertTrue(all(record.levelname == "DEBUG" for record in logs.records))
        missing = False
        self.links["#P0"] = {"discord_user_id": 43}
        with self.assertLogs(logger, level="INFO") as logs:
            self.assertEqual(await self.collect_at(240), ["/players/%23P0", "/players/%23P0/battlelog"])
        self.assertEqual(len(logs.output), 1)

    async def test_all_linked_accounts_use_the_owned_client_and_partial_fields_are_logged(self):
        profile = SimpleNamespace(ok=True, status=200,
            payload={"trophies": 100, "attackWinsRenamed": 20,
                     "leagueTier": {"id": 1, "name": "Synthetic league"},
                     "legendStatistics": {"currentSeason": {"rank": 1, "trophies": 100, "id": "sample"}}})
        self.client.get.side_effect = lambda path, **_: (
            SimpleNamespace(ok=True, status=200, payload={"items": [battle()]})
            if path.endswith("/battlelog") else profile)
        with self.assertLogs("elbow_helper.features.ranked_stats.cog", level="WARNING") as logs:
            await self.cog.collect_once()
        self.assertTrue(any("attackWins" in line for line in logs.output))
        self.assertEqual({call.args[0] for call in self.client.get.await_args_list}, {"/players/%23P0", "/players/%23P2", "/players/%23P0/battlelog", "/players/%23P2/battlelog"})
        result = self.cog.queries.window(after="2026-01-01T00:00:00.000000+00:00",
            before="2027-01-01T00:00:00.000000+00:00", selector="member_ids", values=(42, 43))
        self.assertEqual(len(result["accounts"]), 2)
        for row in result["accounts"]:
            self.assertNotIn("attackWins", row["ranking"])
            self.assertEqual(row["ranking"]["leagueTier"]["name"], "Synthetic league")
            self.assertEqual(row["attacks_used"], 1)

    async def test_accounts_follow_their_own_adaptive_deadlines(self):
        self.client.get.side_effect = self.response
        self.assertEqual(len(await self.collect_at(0)), 4)
        self.assertEqual(await self.collect_at(299), [])
        for seconds in (300, 600, 900):
            self.assertEqual(await self.collect_at(seconds), ["/players/%23P0/battlelog"])
        self.assertCountEqual(await self.collect_at(1200),
                              ["/players/%23P0/battlelog", "/players/%23P2/battlelog"])

    async def test_warns_only_for_nonempty_disjoint_logs_of_the_same_account(self):
        items = [battle(mode="HOME_VILLAGE")]
        self.client.get.side_effect = lambda path, **kwargs: (
            SimpleNamespace(ok=True, status=200, payload={
                "items": [battle(minute=2)] if "P2/" in path else items})
            if path.endswith("/battlelog") else self.response(path, **kwargs))
        logger = "elbow_helper.features.ranked_stats.cog"
        with self.assertNoLogs(logger, level="WARNING"):
            await self.collect_at(0)
            items = [{**battle(mode="HOME_VILLAGE"), "stars": 3}]
            await self.collect_at(300)
        # This battle exists in another account's log, but not this account's last poll.
        self.cog._schedules["#P2"].battle_due = 10**6
        items = [battle(minute=2)]
        with self.assertLogs(logger, level="WARNING") as logs:
            await self.collect_at(600)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("#P0", logs.output[0])
        self.assertIn("may have been missed", logs.output[0])
        with self.assertNoLogs(logger, level="WARNING"):
            items = []
            await self.collect_at(900)
            items = [battle(minute=3)]
            await self.collect_at(1200)

    async def test_failed_log_keeps_previous_successful_log_for_gap_warning(self):
        self.client.get.side_effect = self.response
        await self.collect_at(0)
        self.client.get.side_effect = lambda *_args, **_kwargs: SimpleNamespace(
            ok=False, status=503, payload=None)
        with self.assertNoLogs("elbow_helper.features.ranked_stats.cog", level="WARNING"):
            await self.collect_at(300)
        self.client.get.side_effect = lambda *_args, **_kwargs: SimpleNamespace(
            ok=True, status=200, payload={"items": [battle(minute=30)]})
        with self.assertLogs("elbow_helper.features.ranked_stats.cog", level="WARNING") as logs:
            # The failed poll doubled the interval from 300 to 600 seconds.
            await self.collect_at(900)
        self.assertTrue(any("shares no battles" in line for line in logs.output))

    async def test_profiles_are_hourly_and_can_be_due_without_battle_logs(self):
        self.client.get.side_effect = self.response
        await self.collect_at(0)
        self.assertTrue(all(path.endswith("/battlelog") for path in await self.collect_at(3599)))
        self.assertCountEqual(await self.collect_at(3600), ["/players/%23P0", "/players/%23P2"])
        self.assertEqual(await self.collect_at(3601), [])
        rows, _ = self.cog.store.read_window(after="2000", before="2100", selector="member_ids", values=(42, 43))
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(rows[index + 1]["observed_at"] == stamp(minute=60)
                            for index in (0, 2)))

    async def test_battle_only_polls_reuse_profile_metadata_without_new_snapshots(self):
        self.client.get.side_effect = self.response
        await self.collect_at(0)
        self.client.get.side_effect = lambda *_args, **_kwargs: SimpleNamespace(
            ok=True, status=200, payload={"items": [battle(minute=5)]})
        self.assertEqual(await self.collect_at(300), ["/players/%23P0/battlelog"])
        result = self.cog.queries.window(after=stamp(), before=stamp(4), selector="clan_tags", values=("#Q0",))
        row = next(row for row in result["accounts"] if row["player_tag"] == "#P0")
        self.assertEqual(row["sample_count"], 1)
        self.assertEqual(row["battle_records"], 3)
        self.assertEqual(row["player_name"], "Synthetic account")

    async def test_restart_restores_hourly_and_battle_cooldowns(self):
        self.client.get.side_effect = self.response
        await self.collect_at(0)
        self.cog = RankedStats(self.bot, self.cog.account_links)
        self.assertEqual(await self.collect_at(299), [])
        self.assertEqual(await self.collect_at(300), ["/players/%23P0/battlelog"])
        self.assertCountEqual(await self.collect_at(3600), ["/players/%23P0", "/players/%23P2",
            "/players/%23P0/battlelog", "/players/%23P2/battlelog"])

    async def test_restored_legacy_intervals_are_clamped_to_new_bounds(self):
        for tag, interval in (("#P0", 15), ("#P2", 7200)):
            row = observation(tag)
            poll, _ = parse_log([], row, lambda *_: None)
            poll["poll_interval_seconds"] = interval
            self.cog.store.append([row], polls=[poll])
        self.client.get.side_effect = self.response
        self.assertEqual(await self.collect_at(299), [])
        self.assertEqual(await self.collect_at(300), ["/players/%23P0/battlelog"])
        self.assertCountEqual(await self.collect_at(3600), ["/players/%23P0", "/players/%23P2",
            "/players/%23P0/battlelog", "/players/%23P2/battlelog"])

    async def test_adaptive_changes_and_failed_logs_keep_account_deadlines_independent(self):
        self.client.get.side_effect = self.response
        await self.collect_at(0)
        self.client.get.side_effect = lambda *_args, **_kwargs: SimpleNamespace(
            ok=True, status=200, payload={"items": [battle(minute=0), battle(minute=40)]})
        self.assertEqual(await self.collect_at(300), ["/players/%23P0/battlelog"])
        self.assertEqual(await self.collect_at(600), [])
        self.assertEqual(await self.collect_at(1200), ["/players/%23P2/battlelog"])
        self.client.get.side_effect = lambda *_args, **_kwargs: SimpleNamespace(
            ok=False, status=503, payload=None)
        with self.assertNoLogs("elbow_helper.features.ranked_stats.cog", level="WARNING"):
            self.assertEqual(await self.collect_at(1500), ["/players/%23P0/battlelog"])
        self.assertEqual(await self.collect_at(1800), [])
        with self.assertNoLogs("elbow_helper.features.ranked_stats.cog", level="WARNING"):
            self.assertEqual(await self.collect_at(2400), ["/players/%23P2/battlelog"])
        # Failures still double the interval; the second failure remains below the warning threshold.
        self.assertEqual(await self.collect_at(3500), [])
        with self.assertNoLogs("elbow_helper.features.ranked_stats.cog", level="WARNING"):
            self.assertIn("/players/%23P0/battlelog", await self.collect_at(3900))

    async def test_failed_profiles_are_not_retried_by_more_frequent_battle_polls(self):
        def response(path, **kwargs):
            if not path.endswith("/battlelog"):
                raise RuntimeError("synthetic failure")
            return SimpleNamespace(ok=True, status=200, payload={"items": [battle()]})

        self.client.get.side_effect = response
        with self.assertLogs("elbow_helper.features.ranked_stats.cog"):
            self.assertEqual(len(await self.collect_at(0)), 4)
        self.assertTrue(all(path.endswith("/battlelog") for path in await self.collect_at(300)))
        self.assertTrue(all(path.endswith("/battlelog") for path in await self.collect_at(3599)))
        with self.assertLogs("elbow_helper.features.ranked_stats.cog"):
            self.assertCountEqual(await self.collect_at(3600), ["/players/%23P0", "/players/%23P2"])

    async def test_relinking_preserves_account_cooldown_and_new_accounts_run_immediately(self):
        self.client.get.side_effect = self.response
        await self.collect_at(0)
        del self.links["#P0"]
        self.assertEqual(await self.collect_at(60), [])
        self.assertNotIn("#P0", self.cog._schedules)
        self.links["#P0"] = {"discord_user_id": 44}
        self.links["#P8"] = {"discord_user_id": 45}
        self.assertCountEqual(await self.collect_at(120), ["/players/%23P8", "/players/%23P8/battlelog"])
        self.assertEqual(await self.collect_at(300), ["/players/%23P0/battlelog"])
        _, polls, _ = self.cog.store.read_battle_window(after=stamp(), before=stamp(4), selector="member_ids", values=(44,))
        self.assertEqual(len(polls), 1)

    async def test_request_budget_is_shared_across_accounts_and_both_endpoints(self):
        self.links = {f"#P{first}{second}": {"discord_user_id": index + 1}
                      for index, (first, second) in enumerate(
                          (first, second) for first in "028" for second in "0289")}
        starts = []

        def response(path, **kwargs):
            starts.append((self.seconds, path, kwargs))
            # Failed requests consume the same budget as successful responses.
            if len(starts) % 3 == 0:
                return SimpleNamespace(ok=False, status=503, payload=None)
            return self.response(path, **kwargs)

        self.client.get.side_effect = response
        with self.assertLogs("elbow_helper.features.ranked_stats.cog", level="DEBUG"):
            await self.cog.collect_once()
        self.assertEqual(len(starts), 24)
        self.assertEqual(sum(path.endswith("/battlelog") for _, path, _ in starts), 12)
        self.assertTrue(all(kwargs == {"attempts": 1} for _, _, kwargs in starts))
        for time, _, _ in starts:
            self.assertLessEqual(sum(time <= other < time + 1 for other, _, _ in starts), 5)
        self.assertGreaterEqual(starts[-1][0] - starts[0][0], 4)

    async def test_request_budget_carries_across_batches_and_second_boundaries(self):
        starts = []

        def response(path, **kwargs):
            starts.append(self.seconds)
            return self.response(path, **kwargs)

        self.client.get.side_effect = response
        await self.cog.collect_once()
        self.seconds = 0.9
        await asyncio.gather(*(self.cog._get("/players/%23P0") for _ in range(8)))
        self.assertEqual(len(starts), 12)
        for time in starts:
            self.assertLessEqual(sum(time <= other < time + 1 for other in starts), 5)

    async def test_cancelled_rate_limit_wait_does_not_make_a_request(self):
        self.client.get.side_effect = self.response
        await self.cog.collect_once()
        await self.cog._get("/players/%23P0")
        self.assertEqual(self.client.get.await_count, 5)
        with patch("elbow_helper.features.ranked_stats.cog.asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.cog._get("/players/%23P0")
        self.assertEqual(self.client.get.await_count, 5)
        self.seconds = 1
        await self.cog._get("/players/%23P0")
        self.assertEqual(self.client.get.await_count, 6)

    async def test_failed_log_preserves_stored_battles_and_reports_failed_coverage(self):
        def response(path, **kwargs):
            if path.endswith("/battlelog"):
                return SimpleNamespace(ok=False, status=503, payload=None)
            return SimpleNamespace(ok=True, status=200, payload={"trophies": 100})
        save_log(self.cog.store, [battle()], observation(minute=-5))
        self.client.get.side_effect = response
        with self.assertLogs("elbow_helper.features.ranked_stats.cog"):
            await self.cog.collect_once()
        result = self.cog.queries.window(after=stamp(), before=stamp(4), selector="member_ids", values=(42, 43))
        rows = {row["member_id"]: row for row in result["accounts"]}
        self.assertEqual(rows[42]["attacks_used"], 1)
        self.assertEqual(rows[42]["failed_battle_log_polls"], 1)
        self.assertIsNone(rows[43]["attacks_used"])
        self.assertFalse(result["coverage_complete"])

    async def test_one_failed_account_does_not_stop_other_snapshots(self):
        def response(path, **kwargs):
            if path.endswith("/battlelog"):
                return SimpleNamespace(ok=True, status=200, payload={"items": [battle()]})
            if path.endswith("P0"):
                raise RuntimeError("synthetic failure")
            return SimpleNamespace(ok=True, status=200, payload={"trophies": 100, "attackWins": 0})
        self.client.get.side_effect = response
        with self.assertLogs("elbow_helper.features.ranked_stats.cog"):
            await self.cog.collect_once()
        rows, truncated = self.cog.store.read_window(after="2000", before="2100", selector="member_ids", values=(42, 43))
        self.assertFalse(truncated)
        self.assertEqual(sorted(row["status"] for row in rows), ["ok", "unavailable"])
        records, _, _ = self.cog.store.read_battle_window(after="2000", before="2100", selector="member_ids", values=(42, 43))
        self.assertEqual(len(records), 2)

    async def test_missing_key_skips_collection_without_fabricating_observations(self):
        self.client.configured = False
        await self.cog.collect_once()
        self.client.get.assert_not_awaited()

    async def test_loop_starts_after_initialization_and_cancels_on_unload(self):
        with patch("elbow_helper.features.ranked_stats.cog.start_resilient_loop") as start:
            await self.cog.cog_load()
        start.assert_called_once_with(self.cog.snapshot_loop)
        with patch.object(self.cog.snapshot_loop, "cancel") as cancel:
            self.cog.cog_unload()
        cancel.assert_called_once()
