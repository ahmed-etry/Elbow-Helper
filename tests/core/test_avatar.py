from pathlib import Path
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.core.lifecycle import update_bot_avatar
from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.storage_move import prepare_storage
from elbow_helper.infrastructure.persistence.documents import read_document, write_document
from contextlib import closing
import sqlite3
from tests.temp_directory import TemporaryDirectory


class AvatarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = ApplicationPaths.from_project_root(Path(temporary.name))
        prepare_storage(self.paths.data_root)
        self.paths.avatar_directory.mkdir(parents=True, exist_ok=True)
        self.paths.avatar_file.write_bytes(b"synthetic avatar")
        self.bot = SimpleNamespace(user=SimpleNamespace(edit=AsyncMock()))

    async def test_changed_avatar_reads_and_records_in_workers_after_upload(self):
        write_document(self.paths.avatar_state_file, {"avatar_uploaded_mtime": 0})
        loop_thread = get_ident()
        file_threads = []

        def track(method):
            def wrapped(*args, **kwargs):
                file_threads.append(get_ident())
                return method(*args, **kwargs)
            return wrapped

        async def upload(**fields):
            self.assertEqual(get_ident(), loop_thread)
            self.assertEqual(fields, {"avatar": b"synthetic avatar"})

        self.bot.user.edit.side_effect = upload
        with (
            patch.object(Path, "stat", track(Path.stat)),
            patch.object(Path, "read_text", track(Path.read_text)),
            patch.object(Path, "read_bytes", track(Path.read_bytes)),
            patch.object(Path, "mkdir", track(Path.mkdir)),
            patch.object(Path, "write_text", track(Path.write_text)),
        ):
            await update_bot_avatar(self.bot, self.paths)

        self.assertTrue(file_threads)
        self.assertNotIn(loop_thread, file_threads)
        self.assertEqual(float(read_document(self.paths.avatar_state_file)["avatar_uploaded_mtime"]),
                         self.paths.avatar_file.stat().st_mtime)

    async def test_missing_or_invalid_marker_uploads_and_then_skips_same_file(self):
        for marker in (None, "invalid"):
            with self.subTest(marker=marker):
                with closing(sqlite3.connect(self.paths.avatar_state_file.database)) as connection, connection:
                    connection.execute("DELETE FROM feature_documents WHERE name='application'")
                if marker is not None:
                    write_document(self.paths.avatar_state_file, {"avatar_uploaded_mtime": marker})
                self.bot.user.edit.reset_mock()
                await update_bot_avatar(self.bot, self.paths)
                await update_bot_avatar(self.bot, self.paths)
                self.bot.user.edit.assert_awaited_once_with(avatar=b"synthetic avatar")

    async def test_equal_or_older_avatar_is_not_uploaded(self):
        mtime = self.paths.avatar_file.stat().st_mtime
        for last_upload in (mtime, mtime + 100):
            with self.subTest(last_upload=last_upload):
                write_document(self.paths.avatar_state_file, {"avatar_uploaded_mtime": last_upload})
                await update_bot_avatar(self.bot, self.paths)
        self.bot.user.edit.assert_not_awaited()

    async def test_missing_file_or_user_is_skipped(self):
        self.paths.avatar_file.unlink()
        with self.assertLogs("elbow.boot", level="WARNING"):
            await update_bot_avatar(self.bot, self.paths)
        self.bot.user.edit.assert_not_awaited()
        with self.assertLogs("elbow.boot", level="WARNING"):
            await update_bot_avatar(SimpleNamespace(user=None), self.paths)
        self.assertFalse(self.paths.avatar_state_file.exists())

    async def test_failed_upload_keeps_marker_and_can_retry(self):
        response = SimpleNamespace(status=503, reason="Unavailable", headers={})
        self.bot.user.edit.side_effect = [discord.HTTPException(response, "unavailable"), None]
        write_document(self.paths.avatar_state_file, {"avatar_uploaded_mtime": 0})
        with self.assertLogs("elbow.boot", level="ERROR"):
            await update_bot_avatar(self.bot, self.paths)
        self.assertEqual(read_document(self.paths.avatar_state_file)["avatar_uploaded_mtime"], 0)
        await update_bot_avatar(self.bot, self.paths)
        self.assertEqual(self.bot.user.edit.await_count, 2)
        self.assertNotEqual(read_document(self.paths.avatar_state_file)["avatar_uploaded_mtime"], 0)

    async def test_file_failures_are_logged_and_not_marked_uploaded(self):
        for operation in ("_read_avatar_update", "_save_avatar_upload"):
            with self.subTest(operation=operation):
                self.bot.user.edit.reset_mock()
                with (
                    patch("elbow_helper.core.lifecycle." + operation, side_effect=OSError("denied")),
                    self.assertLogs("elbow.boot", level="ERROR"),
                ):
                    await update_bot_avatar(self.bot, self.paths)
                self.assertFalse(self.paths.avatar_state_file.exists())
                self.assertEqual(self.bot.user.edit.await_count, int(operation == "_save_avatar_upload"))
