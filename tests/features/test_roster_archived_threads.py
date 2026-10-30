from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import patch

import discord

from elbow_helper.features.rosters.repository import RosterRepository
from elbow_helper.features.rosters.services.posts import RosterPostService
from elbow_helper.infrastructure.discord.publishing import PublishingQueue


def http_error(kind=discord.HTTPException, status=400, code=50083):
    return kind(
        SimpleNamespace(status=status, reason="synthetic"),
        {"code": code, "message": "synthetic"},
    )


class RosterArchivedThreadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch("elbow_helper.infrastructure.discord.publishing.HTTP_RETRY_DELAYS", (0, 0)))

    def fixture(self, path, *, archived=True, thread=True, locked=True,
                channel_error=None, message_errors=()):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1, name="Synthetic roster", clan_code="FAMILY",
            role_id=None, max_members=50,
        )
        repository.add_post(roster.id, 111, 222)
        channel = MagicMock(spec=discord.Thread) if thread else SimpleNamespace()
        channel.id = 111
        channel.archived = archived
        channel.locked = locked
        channel.last_message_id = 333 if path == "retire" else 222

        async def history(**kwargs):
            for _ in range(100):
                yield object()

        channel.history = history
        message = SimpleNamespace(id=222, channel=channel, components=[])
        events = []
        errors = iter(message_errors)

        async def edit_channel(**fields):
            events.append("reopen")
            if channel_error is not None:
                raise channel_error
            # discord.py returns a new thread; it need not update this cache.
            return channel

        async def edit_message(**fields):
            events.append("edit")
            error = next(errors, None)
            if error is not None:
                raise error
            return message

        channel.edit = AsyncMock(side_effect=edit_channel)
        channel.fetch_message = AsyncMock(return_value=message)
        message.edit = AsyncMock(side_effect=edit_message)
        queue = PublishingQueue()
        self.addAsyncCleanup(queue.close)
        bot = MagicMock(publishing=queue)
        bot.get_channel.return_value = channel
        posts = RosterPostService(bot, repository, MagicMock(), MagicMock(), MagicMock())
        posts.render = AsyncMock(return_value=([discord.Embed(title="Synthetic")], 0, 1))
        posts.message_view = MagicMock(return_value=None)
        view = MagicMock(spec=discord.ui.View)
        posts._persistent_views[message.id] = view
        return posts, roster, channel, message, events, view

    async def run_path(self, path, posts, roster):
        if path == "disable":
            return await posts.disable_all(roster)
        await posts.refresh(roster)
        return ()

    def assert_posts(self, path, posts, roster, *, success):
        remaining = posts._repository.list_posts(roster.id)
        self.assertEqual(len(remaining), 0 if path == "retire" and success else 1)
        if path == "retire":
            posts.render.assert_not_awaited()

    async def test_archived_threads_reopen_before_every_edit_path_and_preserve_lock(self):
        for path in ("refresh", "retire", "disable"):
            for locked in (True, False):
                with self.subTest(path=path, locked=locked):
                    posts, roster, channel, message, events, view = self.fixture(path, locked=locked)
                    self.assertEqual(await self.run_path(path, posts, roster), ())
                    self.assertEqual(events, ["reopen", "edit"])
                    channel.edit.assert_awaited_once_with(archived=False, reason="Update CWL rosters")
                    self.assertEqual(channel.locked, locked)
                    message.edit.assert_awaited_once()
                    self.assert_posts(path, posts, roster, success=True)
                    if path == "retire":
                        view.stop.assert_called_once_with()

    async def test_stale_open_cache_reopens_then_retries_identical_edit_once(self):
        for path in ("refresh", "retire", "disable"):
            for archived in (True, False):
                with self.subTest(path=path, archived=archived):
                    posts, roster, channel, message, events, _ = self.fixture(
                        path, archived=archived, message_errors=[http_error()],
                    )
                    self.assertEqual(await self.run_path(path, posts, roster), ())
                    expected = ["reopen"] if archived else []
                    self.assertEqual(events, expected + ["edit", "reopen", "edit"])
                    self.assertEqual(channel.edit.await_count, 2 if archived else 1)
                    for call in channel.edit.await_args_list:
                        self.assertEqual(call.kwargs, {"archived": False, "reason": "Update CWL rosters"})
                    self.assertEqual(message.edit.await_count, 2)
                    self.assertEqual(message.edit.await_args_list[0], message.edit.await_args_list[1])
                    self.assert_posts(path, posts, roster, success=True)

    async def test_unarchive_failure_keeps_post_and_view_without_another_edit(self):
        for path in ("refresh", "retire", "disable"):
            for archived in (True, False):
                for error in (http_error(discord.Forbidden, 403, 50013),
                              http_error(discord.NotFound, 404, 10003), http_error(),
                              http_error(status=500, code=0)):
                    with self.subTest(path=path, archived=archived, error=type(error)):
                        posts, roster, channel, message, events, view = self.fixture(
                            path, archived=archived, channel_error=error,
                            message_errors=[] if archived else [http_error()],
                        )
                        failures = await self.run_path(path, posts, roster)
                        self.assertEqual(failures, (222,) if path == "disable" else ())
                        attempts = 3 if error.status == 500 else 1
                        expected = [] if archived else ["edit"]
                        self.assertEqual(events, expected + ["reopen"] * attempts)
                        self.assertEqual(channel.edit.await_count, attempts)
                        self.assertEqual(message.edit.await_count, 0 if archived else 1)
                        self.assert_posts(path, posts, roster, success=False)
                        self.assertIs(posts._persistent_views[222], view)
                        view.stop.assert_not_called()

    async def test_second_archived_error_stops_recovery_and_keeps_post(self):
        for path in ("refresh", "retire", "disable"):
            with self.subTest(path=path):
                posts, roster, channel, message, events, _ = self.fixture(
                    path, archived=False, message_errors=[http_error(), http_error()],
                )
                failures = await self.run_path(path, posts, roster)
                self.assertEqual(failures, (222,) if path == "disable" else ())
                self.assertEqual(events, ["edit", "reopen", "edit"])
                self.assertEqual(channel.edit.await_count, 1)
                self.assertEqual(message.edit.await_count, 2)
                self.assert_posts(path, posts, roster, success=False)

    async def test_normal_channels_and_open_threads_edit_without_reopening(self):
        for path in ("refresh", "retire", "disable"):
            for thread in (True, False):
                with self.subTest(path=path, thread=thread):
                    posts, roster, channel, message, events, _ = self.fixture(
                        path, archived=not thread, thread=thread,
                    )
                    await self.run_path(path, posts, roster)
                    self.assertEqual(events, ["edit"])
                    channel.edit.assert_not_awaited()
                    message.edit.assert_awaited_once()
                    self.assert_posts(path, posts, roster, success=True)

    async def test_reopen_is_sent_again_after_prior_success_with_stale_channel_cache(self):
        posts, roster, channel, message, events, _ = self.fixture(
            "refresh", message_errors=[None, http_error()],
        )
        await posts.refresh(roster)
        channel.archived = False
        posts.render.return_value = ([discord.Embed(title="Changed")], 0, 1)
        await posts.refresh(roster)
        self.assertEqual(events, ["reopen", "edit", "edit", "reopen", "edit"])
        self.assertEqual(channel.edit.await_count, 2)
        self.assertEqual(message.edit.await_count, 3)
        self.assert_posts("refresh", posts, roster, success=True)


if __name__ == "__main__":
    unittest.main()
