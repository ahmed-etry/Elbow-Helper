from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from elbow_helper.features.achievements.definitions import ALL_ACHIEVEMENTS
from elbow_helper.features.achievements.config import (
    DAILY_MIN_CHARS, DAILY_MSG_THRESHOLD, DAILY_REWARD, DAILY_REWARD_ELDER,
    MANUAL_CAP_CWL, MANUAL_CAP_ENCOURAGEMENT, MANUAL_CAP_TOTAL, SALARY_AMOUNT,
    TICKET_COST, TICKET_LIMIT_PER_MONTH,
)
from elbow_helper.features.achievements.definitions import COIN_REWARDS
from elbow_helper.features.achievements.progress import AchievementProgressMixin
from elbow_helper.features.achievements.queries import (
    MAX_COIN_TRANSACTION_PAYLOAD_BYTES, MAX_COIN_TRANSACTIONS,
    AchievementQueries, achievement_leaderboard_eligible,
    achievement_progress_value,
)
from elbow_helper.configuration.roles import LEAD


class _ProgressOwner(AchievementProgressMixin):
    def get_achievement_details(self, achievement_id):
        return next(
            row for row in ALL_ACHIEVEMENTS if row[0] == achievement_id
        )


class AchievementQueriesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "achievements.db"
        self.observed = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript("""
                CREATE TABLE achievements (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    required_count INTEGER,
                    emoji TEXT
                );
                CREATE TABLE user_achievements (
                    user_id INTEGER NOT NULL,
                    achievement_id TEXT NOT NULL,
                    completed_date INTEGER
                );
                CREATE TABLE user_stats (
                    user_id INTEGER PRIMARY KEY,
                    message_count INTEGER,
                    emoji_count INTEGER,
                    reaction_count INTEGER,
                    voice_hours REAL,
                    silent_voice_seconds INTEGER,
                    role_pings INTEGER,
                    meme_posts INTEGER,
                    clan_transfer_count INTEGER,
                    active_channels TEXT,
                    activity_streak INTEGER,
                    weekly_activity_count INTEGER,
                    monthly_activity_count INTEGER,
                    early_bird_count INTEGER,
                    night_owl_count INTEGER
                );
                CREATE TABLE user_coins (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0,
                    last_ticket_month INTEGER
                );
                CREATE TABLE coin_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    reason TEXT,
                    actor_id INTEGER,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE raffle_tickets (
                    month_key INTEGER NOT NULL,
                    user_id INTEGER PRIMARY KEY
                );
                CREATE TABLE raffle_winners (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    month_key INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    drawn_at INTEGER NOT NULL,
                    draw_type TEXT NOT NULL,
                    reroll_of INTEGER,
                    is_active INTEGER NOT NULL
                );
                CREATE TABLE economy_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            connection.executemany(
                "INSERT INTO achievements VALUES (?, ?, ?, ?, ?)",
                [row[:5] for row in ALL_ACHIEVEMENTS],
            )
            connection.execute(
                "INSERT INTO achievements VALUES (?, ?, ?, ?, ?)",
                ("retired_rule", "Retired", "Old rule", 1, ""),
            )
            connection.execute(
                "INSERT INTO user_coins VALUES (?, ?, ?)",
                (42, 125, self.observed.year * 12 + self.observed.month),
            )
            connection.executemany(
                "INSERT INTO coin_transactions "
                "(user_id, amount, type, reason, actor_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ((42, 5, "daily", "daily_drip", None, 1000),
                 (42, -100, "raffle_purchase", "ticket", 42, 1001)),
            )
            connection.executemany(
                "INSERT INTO user_achievements VALUES (?, ?, ?)",
                ((42, "chatterbox", 1000), (43, "chatterbox", 1001),
                 (43, "veteran", 1002), (43, "retired_rule", 1003)),
            )
            connection.execute(
                "INSERT INTO user_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (42, 75, 12, 8, 4.9, 5400, 3, 2, 1,
                 "100 200 300", 4, 6, 30, 2, 5),
            )
            connection.commit()

    def tearDown(self):
        self.temporary.cleanup()

    def test_member_progress_reuses_existing_calculation_values(self):
        joined_at = self.observed - timedelta(days=40)
        queries = AchievementQueries(
            self.path, clock=lambda: self.observed,
        )

        snapshot = queries.member_progress(42, joined_at=joined_at)

        self.assertEqual(snapshot.completed_count, 1)
        self.assertEqual(snapshot.total_count, len(ALL_ACHIEVEMENTS))
        rows = {row.achievement_id: row for row in snapshot.rows}
        self.assertTrue(rows["chatterbox"].completed)
        self.assertEqual(rows["chatterbox"].current_count, 75)
        self.assertEqual(rows["silent_lurker"].current_count, 1.5)
        self.assertEqual(rows["one_of_us"].current_count, 40)
        self.assertEqual(rows["storyteller"].progress_kind, "completion_only")

    def test_shared_progress_helper_matches_existing_command_calculation(self):
        stats = (75, 12, 8, 4.9, 5400, 3, 2, 1,
                 "100 200 300", 4, 6, 30, 2, 5)
        member = SimpleNamespace(
            joined_at=self.observed - timedelta(days=40),
        )
        owner = _ProgressOwner()
        completed = set()
        for achievement_id, *_ in ALL_ACHIEVEMENTS:
            if achievement_id in {"one_of_us", "veteran"}:
                continue
            with self.subTest(achievement_id=achievement_id):
                expected = owner.calculate_achievement_progress(
                    achievement_id, stats, completed, member,
                )
                actual, _ = achievement_progress_value(
                    achievement_id, stats, joined_at=member.joined_at,
                    observed_at=self.observed,
                )
                self.assertEqual(actual, expected["current"])

    def test_leaderboard_counts_known_achievements_without_departed_filtering(self):
        snapshot = AchievementQueries(
            self.path, clock=lambda: self.observed,
        ).leaderboard_counts()

        self.assertEqual(snapshot.total_achievements, len(ALL_ACHIEVEMENTS))
        self.assertEqual(
            [(row.member_id, row.achievement_count) for row in snapshot.rows],
            [(43, 2), (42, 1)],
        )

    def test_missing_database_fails_without_creating_it(self):
        missing = Path(self.temporary.name) / "missing.db"
        with self.assertRaises(RuntimeError):
            AchievementQueries(missing).leaderboard_counts()
        self.assertFalse(missing.exists())

    def test_leaderboard_eligibility_matches_existing_lead_exclusion(self):
        lead_role = next(iter(LEAD))
        self.assertFalse(achievement_leaderboard_eligible({lead_role}))
        self.assertTrue(achievement_leaderboard_eligible(set()))
        self.assertTrue(achievement_leaderboard_eligible(
            {lead_role}, include_leadership=True,
        ))

    def test_inventory_matches_existing_month_key_and_missing_member_defaults(self):
        queries = AchievementQueries(self.path, clock=lambda: self.observed)

        inventory = queries.member_inventory(42)
        missing = queries.member_inventory(999)

        self.assertEqual(inventory.balance, 125)
        self.assertTrue(inventory.has_current_ticket)
        self.assertEqual(
            inventory.current_month_key,
            self.observed.year * 12 + self.observed.month,
        )
        self.assertEqual(missing.balance, 0)
        self.assertFalse(missing.has_current_ticket)

    def test_economy_rules_match_authoritative_feature_configuration(self):
        rules = AchievementQueries(
            self.path, clock=lambda: self.observed,
        ).economy_rules()

        self.assertEqual(rules.observed_at, self.observed.isoformat())
        self.assertEqual(rules.daily_message_threshold, DAILY_MSG_THRESHOLD)
        self.assertEqual(rules.daily_minimum_characters, DAILY_MIN_CHARS)
        self.assertEqual(rules.daily_member_reward, DAILY_REWARD)
        self.assertEqual(rules.daily_elder_reward, DAILY_REWARD_ELDER)
        self.assertEqual(rules.elder_monthly_salary, SALARY_AMOUNT)
        self.assertEqual(rules.ticket_cost, TICKET_COST)
        self.assertEqual(rules.ticket_limit_per_month, TICKET_LIMIT_PER_MONTH)
        self.assertEqual(rules.manual_cwl_monthly_cap, MANUAL_CAP_CWL)
        self.assertEqual(
            rules.manual_encouragement_monthly_cap,
            MANUAL_CAP_ENCOURAGEMENT,
        )
        self.assertEqual(rules.manual_combined_monthly_cap, MANUAL_CAP_TOTAL)
        self.assertEqual(dict(rules.achievement_rewards), COIN_REWARDS)

    def test_coin_history_is_newest_first_with_exact_total(self):
        snapshot = AchievementQueries(
            self.path, clock=lambda: self.observed,
        ).coin_transactions(42)

        self.assertEqual(snapshot.total_transactions, 2)
        self.assertTrue(snapshot.complete)
        self.assertEqual(
            [row.transaction_id for row in snapshot.rows], [2, 1],
        )
        self.assertEqual(snapshot.rows[0].amount, -100)
        self.assertEqual(snapshot.rows[0].actor_id, 42)

    def test_coin_history_reports_bounded_coverage(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executemany(
                "INSERT INTO coin_transactions "
                "(user_id, amount, type, reason, actor_id, created_at) "
                "VALUES (42, 1, 'daily', NULL, NULL, ?)",
                ((2000 + index,) for index in range(MAX_COIN_TRANSACTIONS)),
            )
            connection.commit()

        snapshot = AchievementQueries(self.path).coin_transactions(42)

        self.assertEqual(snapshot.total_transactions, MAX_COIN_TRANSACTIONS + 2)
        self.assertEqual(len(snapshot.rows), MAX_COIN_TRANSACTIONS)
        self.assertFalse(snapshot.complete)

    def test_coin_history_byte_budget_preserves_newest_whole_rows(self):
        reason = "\U0001FA99" * 2_000
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executemany(
                "INSERT INTO coin_transactions "
                "(user_id, amount, type, reason, actor_id, created_at) "
                "VALUES (43, 1, 'manual', ?, NULL, ?)",
                ((reason, 3000 + index) for index in range(100)),
            )
            connection.commit()

        snapshot = AchievementQueries(self.path).coin_transactions(43)

        self.assertEqual(snapshot.total_transactions, 100)
        self.assertLess(len(snapshot.rows), 100)
        self.assertFalse(snapshot.complete)
        self.assertGreater(len(snapshot.rows), 0)
        self.assertLessEqual(
            sum(len(row.reason.encode("utf-8")) for row in snapshot.rows),
            MAX_COIN_TRANSACTION_PAYLOAD_BYTES,
        )
        self.assertGreater(
            snapshot.rows[0].transaction_id,
            snapshot.rows[-1].transaction_id,
        )

    def test_raffle_reads_exact_month_scope_and_existing_defaults(self):
        month_key = 2026 * 12 + 8
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executemany(
                "INSERT INTO economy_meta VALUES (?, ?)",
                ((f"reward_{month_key}", "Gold Pass"),
                 (f"winners_{month_key}", "2")),
            )
            connection.executemany(
                "INSERT INTO raffle_tickets VALUES (?, ?)",
                ((month_key, 42), (month_key, 43)),
            )
            connection.executemany(
                "INSERT INTO raffle_winners "
                "(month_key, user_id, drawn_at, draw_type, reroll_of, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ((month_key, 42, 1000, "draw", None, 0),
                 (month_key, 43, 1001, "reroll", 1, 1)),
            )
            connection.commit()

        snapshot = AchievementQueries(
            self.path, clock=lambda: self.observed,
        ).raffle("2026-08")

        self.assertEqual(snapshot.month_key, month_key)
        self.assertEqual(snapshot.month_label, "August 2026")
        self.assertEqual(snapshot.prize, "Gold Pass")
        self.assertEqual(snapshot.configured_winners, 2)
        self.assertEqual(snapshot.ticket_member_ids, (42, 43))
        self.assertEqual([row.member_id for row in snapshot.winners], [42, 43])
        self.assertFalse(snapshot.winners[0].active)
        self.assertTrue(snapshot.winners[1].active)
        self.assertTrue(snapshot.complete)

        empty = AchievementQueries(
            self.path, clock=lambda: self.observed,
        ).raffle()
        self.assertEqual(empty.month_label, "September 2026")
        self.assertEqual(empty.configured_winners, 1)
        self.assertEqual(empty.ticket_member_ids, ())

    def test_raffle_rejects_invalid_months(self):
        queries = AchievementQueries(self.path)
        for value in ("2026-00", "2026-13", "26-09", "2026/09"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                queries.raffle(value)


if __name__ == "__main__":
    unittest.main()
