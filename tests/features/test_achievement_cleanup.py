from contextlib import closing
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sqlite3
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.achievements.schema import DATABASE
from elbow_helper.features.achievements.cog import Achievements
from elbow_helper.features.achievements.tracking import AchievementTrackingMixin
from tests.temp_directory import TemporaryDirectory


class _Tracking(AchievementTrackingMixin):
    GUILD_ID = 1


class AchievementCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_transactions_leave_the_loop_and_preserve_member_write_order(self):
        member = SimpleNamespace(id=1, roles=[], bot=False)
        mentioned = SimpleNamespace(id=2, roles=[], bot=False)
        guild = SimpleNamespace(get_member=lambda user_id: {1: member, 2: mentioned}.get(user_id))
        cog = Achievements(SimpleNamespace(paths=SimpleNamespace(data_root=self.path.parent),
                                          get_guild=lambda _: guild))
        cog.init_database()
        cog.init_achievements()
        with closing(sqlite3.connect(cog.db_path)) as connection, connection:
            connection.execute('INSERT INTO user_stats(user_id,message_count) VALUES (1,98)')
        loop_thread = get_ident()
        db_threads = []
        notification_threads = []
        message_counts = []
        cog.send_achievement_notification = AsyncMock(side_effect=lambda *args: notification_threads.append(get_ident()))
        connect = DATABASE.connect

        class Cursor(sqlite3.Cursor):
            def execute(self, sql, parameters=()):
                db_threads.append(get_ident())
                if sql.startswith('UPDATE user_stats SET message_count ='):
                    message_counts.append(parameters[0])
                return super().execute(sql, parameters)

            def fetchone(self):
                db_threads.append(get_ident())
                return super().fetchone()

        class Connection(sqlite3.Connection):
            def cursor(self):
                db_threads.append(get_ident())
                return super().cursor(factory=Cursor)

            def commit(self):
                db_threads.append(get_ident())
                return super().commit()

            def close(self):
                db_threads.append(get_ident())
                return super().close()

        def observed_connect(*args, **kwargs):
            db_threads.append(get_ident())
            return connect(*args, factory=Connection, **kwargs)

        now = datetime.now(timezone.utc)
        messages = [SimpleNamespace(author=member, guild=guild, channel=SimpleNamespace(id=1),
                    content='A message long enough for daily activity', attachments=[], mentions=[mentioned],
                    created_at=now + timedelta(minutes=index)) for index in range(2)]
        with patch.object(DATABASE, 'connect', side_effect=observed_connect):
            await asyncio.gather(*(cog.on_message(message) for message in messages))
        self.assertTrue(db_threads)
        self.assertNotIn(loop_thread, db_threads)
        self.assertEqual(message_counts, [99, 100])
        self.assertEqual(notification_threads, [loop_thread])
        with closing(sqlite3.connect(cog.db_path)) as connection:
            self.assertEqual(connection.execute('SELECT message_count FROM user_stats WHERE user_id=1').fetchone(), (100,))
            self.assertEqual(connection.execute('SELECT role_pings FROM user_stats WHERE user_id=2').fetchone(), (2,))
            self.assertEqual(connection.execute('SELECT daily_msg_count FROM user_coins WHERE user_id=1').fetchone(), (2,))

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
