from contextlib import closing
import logging
from pathlib import Path
import sqlite3
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.features.achievements.schema import DATABASE
from elbow_helper.features.achievements.tracking import AchievementTrackingMixin
from tests.temp_directory import TemporaryDirectory


class _Tracking(AchievementTrackingMixin):
    GUILD_ID = 1


class AchievementCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "achievements.db"
        self.tracking = _Tracking()
        self.tracking.db_path = self.path
        self.tracking.logger = logging.getLogger("test.achievement_cleanup")
        self.guild = SimpleNamespace(members=[SimpleNamespace(id=1)])
        self.tracking.bot = SimpleNamespace(get_guild=lambda _: self.guild)
        with closing(DATABASE.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO achievements(id,name,description) VALUES ('synthetic','Synthetic','Synthetic')")
            for member_id in (1, 2):
                connection.execute("INSERT INTO user_stats(user_id) VALUES (?)", (member_id,))
                connection.execute("INSERT INTO user_achievements VALUES (?,'synthetic',1)", (member_id,))
                connection.execute("INSERT INTO user_coins(user_id,balance) VALUES (?,50)", (member_id,))

    def members_in(self, table):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(f"SELECT user_id FROM {table} ORDER BY user_id").fetchall()

    async def test_cleanup_runs_sql_in_worker_and_keeps_current_members_and_coins(self):
        loop_thread = get_ident()
        threads = []
        connect = DATABASE.connect

        def observed_connect(*args, **kwargs):
            threads.append(get_ident())
            return connect(*args, **kwargs)

        with patch.object(DATABASE, "connect", side_effect=observed_connect):
            await self.tracking.cleanup_database()
            await self.tracking.cleanup_database()
        self.assertEqual(len(threads), 2)
        self.assertNotIn(loop_thread, threads)
        self.assertEqual(self.members_in("user_stats"), [(1,)])
        self.assertEqual(self.members_in("user_achievements"), [(1,)])
        self.assertEqual(self.members_in("user_coins"), [(1,), (2,)])

    async def test_failed_second_delete_rolls_back_and_next_cleanup_can_retry(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""CREATE TRIGGER refuse_delete BEFORE DELETE ON user_achievements
                BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        with self.assertLogs(self.tracking.logger, level="ERROR"):
            await self.tracking.cleanup_database()
        self.assertEqual(self.members_in("user_stats"), [(1,), (2,)])
        self.assertEqual(self.members_in("user_achievements"), [(1,), (2,)])
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TRIGGER refuse_delete")
        await self.tracking.cleanup_database()
        self.assertEqual(self.members_in("user_stats"), [(1,)])

    async def test_unavailable_database_is_logged_without_deleting_rows(self):
        with (
            patch.object(DATABASE, "connect", side_effect=sqlite3.OperationalError("locked")),
            self.assertLogs(self.tracking.logger, level="ERROR"),
        ):
            await self.tracking.cleanup_database()
        self.assertEqual(self.members_in("user_stats"), [(1,), (2,)])
