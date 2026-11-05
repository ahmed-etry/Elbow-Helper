"""Family-record profile schema upgrades preserve facts and roll back failures."""

from contextlib import closing
import sqlite3
import unittest
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.schema import MIGRATIONS
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration, SQLiteMigrationError, run_sqlite_migrations
from .test_wars import WarFixture


class ProfileUpgradeTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def test_profile_migration_failure_rolls_back_accounts_and_schema(self):
        path = self.path.parent / "failed-migration.db"
        with closing(sqlite3.connect(path)) as c:
            run_sqlite_migrations(c, MIGRATIONS[:11], target_version=11)
            c.execute("INSERT INTO accounts VALUES (1,'#P0','Name',123,NULL,'observed')")
            c.commit()
            before = tuple(c.iterdump())
            with self.assertRaises(SQLiteMigrationError):
                def fail(connection):
                    MIGRATIONS[11].apply(connection)
                    raise ValueError("forced_migration_failure")
                run_sqlite_migrations(c, (*MIGRATIONS[:11], SQLiteMigration(12, "failure", fail)), target_version=12)
            self.assertEqual(tuple(c.iterdump()), before)

    async def test_accounts_migration_preserves_dates_and_references(self):
        path = self.path.parent / "v11.db"
        with closing(sqlite3.connect(path)) as c:
            c.execute("PRAGMA foreign_keys=ON")
            run_sqlite_migrations(c, MIGRATIONS[:11], target_version=11)
            c.execute("INSERT INTO accounts VALUES (1,'#P0','Name',123,NULL,'observed')")
            c.commit()
        store = FamilyStore(path)
        await store.initialize()
        rows = await store.call(lambda c: [tuple(r) for r in c.execute("SELECT * FROM accounts")])
        self.assertEqual(rows, [(1, "#P0", "Name", 123, None, "observed")])
