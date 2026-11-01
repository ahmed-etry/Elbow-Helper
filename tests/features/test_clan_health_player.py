from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from elbow_helper.features.clan_health.analysis import ClanHealthAnalyzer
from elbow_helper.features.clan_health.config import UTC
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.clan_health.database.schema import create_schema
from elbow_helper.features.clan_health.commands.player import ClanHealthPlayerCommandMixin


class PlayerMovementHistoryTests(unittest.TestCase):
    def test_zero_lookback_uses_only_the_selected_period(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "clan_health.db"
            with sqlite3.connect(database_path) as connection:
                create_schema(connection)
                connection.executemany(
                    "INSERT INTO player_snapshots(player_tag, captured_ts, clan_code, player_name) VALUES (?, ?, ?, ?)",
                    [
                        ("#PLAYER", 1_700_000_000, "OLD", "Player"),
                        ("#PLAYER", 1_700_100_000, "CURRENT", "Player"),
                        ("#PLAYER", 1_700_200_000, "CURRENT", "Player"),
                    ],
                )
            connection.close()

            segments = ClanHealthRepository(
                database_path
            ).player_movement(
                player_tag="#PLAYER",
                cycle_start=datetime.fromtimestamp(
                    1_700_100_000,
                    tz=UTC,
                ),
                cycle_end=datetime.fromtimestamp(
                    1_700_200_000,
                    tz=UTC,
                ),
                lookback_days=0,
            )

        self.assertEqual([segment["clan_code"] for segment in segments], ["CURRENT"])


class RegularWarsStub:
    """The family record's regular wars as clan health asks for them."""

    def __init__(self, activity=None, counts=None):
        self.activity_rows = activity or {}
        self.counts = counts or {}
        self.calls = []

    def activity(self, **window):
        self.calls.append(("activity", window))
        return {tag: row for tag, row in self.activity_rows.items() if tag in window["player_tags"]}

    def war_counts(self, *, clan_codes=None, **window):
        self.calls.append(("war_counts", {**window, "clan_codes": clan_codes}))
        return {code: count for code, count in self.counts.items() if clan_codes is None or code in clan_codes}


def regular(wars, expected, used, *, stars=0, destruction=0.0, measured=0, unknown=0):
    return {"wars": wars, "attacks_expected": expected, "attacks_used": used, "stars": stars,
            "destruction": destruction, "attacks_measured": measured, "wars_without_counts": unknown}


class ClanHealthAnalyzerFacadeTests(unittest.TestCase):
    def test_sparse_report_check_keeps_static_calling_contract(self) -> None:
        analyzer = ClanHealthAnalyzer(
            ClanHealthRepository(Path("unused-clan-health.db")), RegularWarsStub(),
        )

        self.assertTrue(analyzer.report_row_is_sparse(None))
        self.assertFalse(
            analyzer.report_row_is_sparse({"donations": 1})
        )


class FamilyWarParticipationTests(unittest.TestCase):
    def test_regular_wars_from_the_record_join_stored_league_wars(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / "health.sqlite3")
            repository.initialize()
            repository.store_war_activity([
                {"war_id": f"CWL:#W{index}", "war_type": "CWL", "clan_code": "BES", "clan_tag": "#P2",
                 "end_ts": 100 + index, "player_tag": "#P0", "player_name": "Synthetic",
                 "attacks_expected": 1, "attacks_used": 1 - index % 2, "stars": 2.0, "destruction": 80.0,
                 "attack_count": 1 - index % 2}
                for index in range(3)
            ] + [
                # Old regular rows are no longer read.
                {"war_id": "REG:#OLD", "war_type": "REG", "clan_code": "BES", "clan_tag": "#P2", "end_ts": 150,
                 "player_tag": "#P0", "player_name": "Synthetic", "attacks_expected": 2, "attacks_used": 0},
            ])
            stub = RegularWarsStub({"#P0": regular(2, 4, 3, stars=7, destruction=250.0, measured=3)})
            analyzer = ClanHealthAnalyzer(repository, stub)
            rows = [{"player_tag": "#P0", "clan_code": "BES"}, {"player_tag": "#P8", "clan_code": "BES"}]
            start, end = (datetime.fromtimestamp(value, tz=UTC) for value in (1, 200))
            analyzer._apply_family_war_activity(rows, cycle_start=start, cycle_end=end)
            self.assertEqual(stub.calls, [("activity", {"cycle_start_ts": 1, "cycle_end_ts": 200,
                                                        "player_tags": {"#P0", "#P8"}})])
            row = rows[0]
            self.assertEqual((row["regular_war_events_joined"], row["regular_war_hits_expected"],
                              row["regular_war_hits_used"], row["regular_war_missed"]), (2, 4, 3, 1))
            self.assertEqual((row["cwl_events_joined"], row["cwl_hits_expected"], row["cwl_hits_used"],
                              row["cwl_missed"]), (3, 3, 2, 1))
            self.assertEqual((row["war_events_joined"], row["war_hits_expected"], row["war_hits_used"],
                              row["war_missed"]), (5, 7, 5, 2))
            self.assertEqual((row["war_stars_total"], row["war_destruction_total"], row["war_attack_count"]),
                             (13.0, 490.0, 5))
            # No war at all leaves the account's other war fields as they were.
            self.assertEqual(rows[1], {"player_tag": "#P8", "clan_code": "BES", "war_events_joined": 0})

    def test_a_regular_war_with_unknown_counts_is_joined_but_never_missed(self):
        stub = RegularWarsStub({"#P0": regular(1, 0, 0, unknown=1)})
        repository = SimpleNamespace(cwl_war_activity=lambda **_: {})
        rows = [{"player_tag": "#P0", "clan_code": "BES"}]
        start, end = (datetime.fromtimestamp(value, tz=UTC) for value in (1, 200))
        ClanHealthAnalyzer(repository, stub)._apply_family_war_activity(rows, cycle_start=start, cycle_end=end)
        self.assertEqual((rows[0]["regular_war_events_joined"], rows[0]["war_hits_expected"], rows[0]["war_missed"]),
                         (1, 0, 0))

    def test_home_and_extra_wars_do_not_share_a_home_only_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / "health.sqlite3")
            repository.initialize()
            stub = RegularWarsStub({"#P0": regular(15, 30, 30)}, {"BES": 5, "BEW": 10})
            analyzer = ClanHealthAnalyzer(repository, stub)
            rows = [{"player_tag": "#P0", "player_name": "Synthetic", "clan_code": "BES"}]
            start, end = (datetime.fromtimestamp(value, tz=UTC) for value in (1, 200))
            analyzer._apply_family_war_activity(rows, cycle_start=start, cycle_end=end)
            rules = {"_profile_name": "competitive", "war": {
                "wars_to_join": 5, "missed_attack_rate_percent": 0,
            }}
            with patch("elbow_helper.features.clan_health.analysis.flags.effective_player_rules",
                       return_value=rules):
                analyzer._apply_flags(rows, cycle_start=start, cycle_end=end)
            # Only the player's own clan's wars set the regular-war denominator.
            self.assertEqual(stub.calls[-1], ("war_counts", {"cycle_start_ts": 1, "cycle_end_ts": 200,
                                                             "clan_codes": {"BES"}}))
            row = rows[0]
            self.assertEqual(row["regular_war_events_joined"], 15)
            self.assertEqual((row["regular_war_hits_expected"], row["regular_war_hits_used"]), (30, 30))
            card = row["signal_cards"]["war_attendance"]
            expected = "Joined 15 regular wars across the family"
            self.assertEqual(card["current"], expected)
            self.assertEqual(ClanHealthPlayerCommandMixin._signal_summary_text(card), expected)
            self.assertEqual(card["verdict"], "Good")
            self.assertEqual(row["war_hit_usage_verdict"], "Good")

    def test_one_and_no_regular_wars_have_matching_report_wording(self):
        repository = SimpleNamespace(cwl_war_counts=lambda **_: {})
        rules = {"_profile_name": "casual", "war": {"wars_to_join": 0}}
        for count, expected in ((1, "Joined 1 regular war across the family"),
                                (0, "No regular wars joined during this period")):
            with self.subTest(count=count), patch(
                "elbow_helper.features.clan_health.analysis.flags.effective_player_rules", return_value=rules,
            ):
                rows = [{"player_tag": "#P0", "clan_code": "BES", "regular_war_events_joined": count}]
                ClanHealthAnalyzer(repository, RegularWarsStub())._apply_flags(rows)
                card = rows[0]["signal_cards"]["war_attendance"]
                self.assertEqual(card["current"], expected)
                self.assertEqual(card["reason"], "Regular wars are not required for this clan.")
