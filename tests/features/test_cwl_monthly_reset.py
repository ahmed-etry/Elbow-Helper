from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.features.cwl.threads.board import CwlThreadBoardMixin
from elbow_helper.features.cwl.threads.tasks import CwlThreadTasksMixin


NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)


def http_error(status=503, code=0):
    cls = {403: discord.Forbidden, 404: discord.NotFound}.get(status, discord.HTTPException)
    return cls(SimpleNamespace(status=status, reason="Synthetic failure"),
               {"code": code, "message": "Synthetic failure"})


class ResetHarness(CwlThreadBoardMixin, CwlThreadTasksMixin):
    def __init__(self):
        self.data = {"threads": {"123": {
            "clan_name": "Synthetic clan", "sticky_message_id": 900,
            "cc_status": {"filled": True}, "cc_statuses": {"war": "filled"},
            "active_prep": {"war_tag": "#WAR"}, "last_activity": "before",
        }}, "settings": {"auto_reset_last_ym": "2026-10"}}
        self.saved = None
        self.save_data = MagicMock(side_effect=self.save)
        self._current_auto_reset_month = lambda: "2026-11"
        self._utc_now = lambda: NOW
        self._utc_now_iso = lambda: NOW.isoformat()
        self._locks = {}
        self.messages = []
        self.history_error = None
        self.bulk_delete = AsyncMock(side_effect=self.delete_batch)
        self.thread = self.new_thread(archived=True)
        self.bot = SimpleNamespace(
            get_channel=lambda _: self.thread, fetch_channel=AsyncMock(),
            publishing=SimpleNamespace(edit_channel=AsyncMock(side_effect=self.edit_thread)),
        )
        self.first = self.message(1)
        self.board = self.message(900)
        self.extra = self.message(2)

    def save(self):
        self.saved = deepcopy(self.data)

    def _get_sticky_lock(self, thread_id):
        return self._locks.setdefault(thread_id, asyncio.Lock())

    def message(self, message_id, *, days=1, pinned=False):
        message = SimpleNamespace(id=message_id, pinned=pinned,
                                  created_at=NOW - timedelta(days=days))

        async def delete():
            self.messages.remove(message)

        message.delete = AsyncMock(side_effect=delete)
        self.messages.append(message)
        return message

    async def delete_batch(self, batch):
        for message in batch:
            if message in self.messages:
                self.messages.remove(message)

    def new_thread(self, *, archived, locked=False):
        thread = MagicMock(spec=discord.Thread)
        thread.id, thread.name = 123, "Synthetic thread"
        thread.archived, thread.locked = archived, locked
        thread.delete_messages = self.bulk_delete

        async def history(*, limit, oldest_first=False):
            if self.history_error:
                raise self.history_error
            messages = list(self.messages)
            if not oldest_first:
                messages.reverse()
            for message in messages if limit is None else messages[:limit]:
                yield message

        async def fetch(message_id):
            for message in self.messages:
                if message.id == message_id:
                    return message
            raise http_error(404)

        thread.history = history
        thread.fetch_message = AsyncMock(side_effect=fetch)
        return thread

    async def edit_thread(self, thread, *, archived, reason, locked=False):
        self.thread = self.new_thread(archived=archived, locked=locked)
        return SimpleNamespace(value=self.thread)


class CwlMonthlyResetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = ResetHarness()
        self.sleep = patch("elbow_helper.features.cwl.threads.tasks.asyncio.sleep", new_callable=AsyncMock)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    async def reset(self):
        await CwlThreadTasksMixin.auto_reset_cwl.coro(self.manager)

    def assert_pending(self):
        self.assertEqual(self.manager.data["settings"]["auto_reset_last_ym"], "2026-10")
        entry = self.manager.data["threads"]["123"]
        self.assertEqual(entry["active_prep"], {"war_tag": "#WAR"})
        self.assertEqual(entry["cc_status"], {"filled": True})
        self.assertEqual(entry["cc_statuses"], {"war": "filled"})
        self.assertEqual(entry["last_activity"], "before")

    def assert_complete(self):
        self.assertEqual(self.manager.saved["settings"]["auto_reset_last_ym"], "2026-11")
        entry = self.manager.saved["threads"]["123"]
        self.assertNotIn("active_prep", entry)
        self.assertEqual(entry["cc_status"], {})
        self.assertEqual(entry["cc_statuses"], {})
        self.assertTrue(self.manager.thread.archived)

    async def test_reset_carries_fresh_open_thread_through_cleanup_and_close(self):
        original = self.manager.thread
        await self.reset()
        edits = self.manager.bot.publishing.edit_channel.await_args_list
        self.assertEqual([call.kwargs["archived"] for call in edits], [False, True])
        self.assertIs(edits[0].args[0], original)
        self.assertIsNot(edits[1].args[0], original)
        self.assertFalse(edits[1].args[0].archived)
        self.assertEqual(self.manager.messages, [self.manager.first])
        self.assert_complete()
        await self.reset()
        self.assertEqual(self.manager.bot.publishing.edit_channel.await_count, 2)
        self.manager.board.delete.assert_awaited_once()

    async def test_history_failure_keeps_state_and_retry_skips_already_deleted_board(self):
        self.manager.history_error = http_error()
        await self.reset()
        self.assert_pending()
        self.assertTrue(self.manager.thread.archived)
        self.manager.board.delete.assert_awaited_once()
        self.assertIsNone(self.manager.saved["threads"]["123"]["sticky_message_id"])
        self.manager.history_error = None
        await self.reset()
        self.assert_complete()
        self.manager.board.delete.assert_awaited_once()

    async def test_message_delete_failure_keeps_month_pending_until_retry(self):
        self.manager.bulk_delete.side_effect = http_error()
        await self.reset()
        self.assert_pending()
        self.assertTrue(self.manager.thread.archived)
        self.manager.bulk_delete.side_effect = self.manager.delete_batch
        await self.reset()
        self.assert_complete()
        self.manager.board.delete.assert_awaited_once()

    async def test_stale_board_delete_failure_retries_after_main_board_is_gone(self):
        stale = self.manager.message(901, pinned=True)
        self.manager.data["threads"]["123"]["stale_sticky_message_ids"] = [901]
        delete = stale.delete.side_effect
        stale.delete.side_effect = http_error(403)
        await self.reset()
        self.assert_pending()
        self.assertIsNone(self.manager.saved["threads"]["123"]["sticky_message_id"])
        self.assertEqual(self.manager.saved["threads"]["123"]["stale_sticky_message_ids"], [901])
        stale.delete.side_effect = delete
        await self.reset()
        self.assert_complete()
        self.assertNotIn("stale_sticky_message_ids", self.manager.saved["threads"]["123"])
        self.assertNotIn(stale, self.manager.messages)
        self.manager.board.delete.assert_awaited_once()

    async def test_retry_leaves_finished_threads_alone(self):
        finished = {"clan_name": "Other clan", "cc_status": {"filled": True}, "cc_statuses": {},
                    "active_prep": {"war_tag": "#OTHER"}, "last_activity": "before"}
        self.manager.data["threads"]["456"] = finished
        self.manager.clear_thread_messages_except_first = AsyncMock(side_effect=[False, True, True])
        await self.reset()
        self.assertEqual(self.manager.data["settings"]["auto_reset_last_ym"], "2026-10")
        self.assertEqual(self.manager.data["threads"]["123"]["active_prep"], {"war_tag": "#WAR"})
        self.assertNotIn("active_prep", finished)
        await self.reset()
        self.assertEqual(self.manager.clear_thread_messages_except_first.await_count, 3)
        self.assertNotIn("active_prep", self.manager.data["threads"]["123"])
        self.assertEqual(self.manager.saved["settings"]["auto_reset_last_ym"], "2026-11")

    async def test_archive_helper_keeps_current_thread_when_no_edit_is_needed(self):
        thread = self.manager.thread
        self.assertIs(await self.manager._set_registered_thread_archived(
            thread, archived=True, reason="Synthetic close"), thread)
        thread.archived = False
        self.assertIs(await self.manager._set_registered_thread_archived(
            thread, archived=False, reason="Synthetic open"), thread)
        self.manager.bot.publishing.edit_channel.assert_not_awaited()

    async def test_locked_thread_is_opened_and_unlocked_before_cleanup(self):
        self.manager.thread = self.manager.new_thread(archived=False, locked=True)
        await self.reset()
        edits = self.manager.bot.publishing.edit_channel.await_args_list
        self.assertFalse(edits[0].kwargs["archived"])
        self.assertFalse(edits[0].kwargs["locked"])
        self.assertFalse(edits[1].args[0].locked)
        self.assert_complete()

    async def test_board_delete_failure_keeps_state_and_retries(self):
        delete = self.manager.board.delete.side_effect
        self.manager.board.delete.side_effect = http_error(403)
        await self.reset()
        self.assert_pending()
        self.assertIn(self.manager.board, self.manager.messages)
        self.manager.board.delete.side_effect = delete
        await self.reset()
        self.assert_complete()
        self.assertNotIn(self.manager.board, self.manager.messages)

    async def test_missing_thread_keeps_state_and_retries(self):
        thread = self.manager.thread
        self.manager.thread = None
        self.manager.bot.fetch_channel.side_effect = http_error(403)
        await self.reset()
        self.assert_pending()
        self.manager.thread = thread
        await self.reset()
        self.assert_complete()

    async def test_failed_open_or_close_keeps_state_and_retries(self):
        for fail_open in (True, False):
            with self.subTest(fail_open=fail_open):
                self.manager = ResetHarness()
                edit = self.manager.edit_thread

                async def fail(thread, **kwargs):
                    if kwargs["archived"] != fail_open:
                        return None
                    return await edit(thread, **kwargs)

                self.manager.bot.publishing.edit_channel.side_effect = fail
                await self.reset()
                self.assert_pending()
                if fail_open:
                    self.manager.board.delete.assert_not_awaited()
                self.manager.bot.publishing.edit_channel.side_effect = edit
                await self.reset()
                self.assert_complete()


class CwlMessageCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = ResetHarness()
        self.manager.messages = [self.manager.first]
        self.sleep = patch("elbow_helper.features.cwl.threads.tasks.asyncio.sleep", new_callable=AsyncMock)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    async def clear(self):
        return await self.manager.clear_thread_messages_except_first(self.manager.thread)

    async def test_empty_and_protected_messages_are_successful(self):
        pinned = self.manager.message(3, pinned=True)
        sticky = self.manager.message(900)
        self.manager.message(4)
        self.manager.message(5, days=20)
        self.assertTrue(await self.clear())
        self.assertEqual(self.manager.messages, [self.manager.first, pinned, sticky])
        self.manager.messages.clear()
        self.assertTrue(await self.clear())

    async def test_old_message_failure_is_reported_and_retry_finishes(self):
        old = self.manager.message(3, days=20)
        delete = old.delete.side_effect
        old.delete.side_effect = http_error(403)
        self.assertFalse(await self.clear())
        self.assertIn(old, self.manager.messages)
        old.delete.side_effect = delete
        self.assertTrue(await self.clear())
        self.assertEqual(self.manager.messages, [self.manager.first])

    async def test_already_deleted_old_message_is_successful(self):
        self.manager.message(3, days=20).delete.side_effect = http_error(404)
        self.assertTrue(await self.clear())

    async def test_bulk_failure_in_full_or_last_batch_is_reported_and_retry_finishes(self):
        for count in (2, 101):
            with self.subTest(count=count):
                self.manager = ResetHarness()
                self.manager.messages = [self.manager.first]
                for message_id in range(10, 10 + count):
                    self.manager.message(message_id)
                self.manager.bulk_delete.side_effect = http_error()
                self.assertFalse(await self.clear())
                self.manager.bulk_delete.side_effect = self.manager.delete_batch
                self.assertTrue(await self.clear())
                self.assertEqual(self.manager.messages, [self.manager.first])

    async def test_age_limit_fallback_reports_individual_failure_and_retries(self):
        for count in (2, 101):
            with self.subTest(count=count):
                self.manager = ResetHarness()
                self.manager.messages = [self.manager.first]
                messages = [self.manager.message(i) for i in range(10, 10 + count)]
                failed = messages[-1]
                delete = failed.delete.side_effect
                failed.delete.side_effect = http_error(403)
                self.manager.bulk_delete.side_effect = http_error(400, code=50034)
                self.assertFalse(await self.clear())
                self.assertEqual(self.manager.messages, [self.manager.first, failed])
                failed.delete.side_effect = delete
                self.assertTrue(await self.clear())
                self.assertEqual(self.manager.messages, [self.manager.first])

    async def test_age_limit_fallback_accepts_already_deleted_messages(self):
        for count in (2, 101):
            with self.subTest(count=count):
                self.manager = ResetHarness()
                self.manager.messages = [self.manager.first]
                messages = [self.manager.message(i) for i in range(10, 10 + count)]
                messages[-1].delete.side_effect = http_error(404)
                self.manager.bulk_delete.side_effect = http_error(400, code=50034)
                self.assertTrue(await self.clear())


if __name__ == "__main__":
    unittest.main()
