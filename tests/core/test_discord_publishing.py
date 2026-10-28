from __future__ import annotations

import asyncio
from io import BytesIO
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.infrastructure.discord.publishing import PublishingQueue
from elbow_helper.infrastructure.discord.interactions import edit_bound_view
from elbow_helper.infrastructure.discord.interactions import edit_original_bound_view


def http_error(kind=discord.HTTPException, status=500):
    return kind(SimpleNamespace(status=status, reason="synthetic"), "synthetic")


class PublishingQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.queue = PublishingQueue()
        self.addAsyncCleanup(self.queue.close)

    def target(self, id=10):
        target = SimpleNamespace(id=id)
        target.edit = AsyncMock(return_value=target)
        return target

    async def test_five_pending_versions_send_only_newest_and_resolve_together(self):
        message = self.target()
        versions = [self.queue.edit_message(message, content=str(index)) for index in range(5)]
        results = await asyncio.gather(*versions)
        message.edit.assert_awaited_once_with(content="4")
        self.assertTrue(all(results))
        self.assertTrue(all(result is results[0] for result in results))
        self.assertIs(results[0].value, message)

    async def test_inflight_version_finishes_independently_of_pending_versions(self):
        started, finish = asyncio.Event(), asyncio.Event()
        message = self.target()

        async def edit(**fields):
            if fields["content"] == "first":
                started.set()
                await finish.wait()
            return fields["content"]

        message.edit.side_effect = edit
        first = self.queue.edit_message(message, content="first")
        await started.wait()
        second = self.queue.edit_message(message, content="second")
        newest = self.queue.edit_message(message, content="newest")
        self.assertFalse(second.done())
        finish.set()
        results = await asyncio.gather(first, second, newest)
        self.assertEqual([result.value for result in results], ["first", "newest", "newest"])
        self.assertEqual(message.edit.await_count, 2)

    async def test_cancelled_caller_does_not_cancel_other_versions(self):
        message = self.target()
        cancelled = self.queue.edit_message(message, content="old")
        newest = self.queue.edit_message(message, content="new")
        cancelled.cancel()
        self.assertTrue(await newest)
        message.edit.assert_awaited_once_with(content="new")

    async def test_channel_fields_merge_with_newest_value_per_field(self):
        channel = self.target()
        versions = [self.queue.edit_channel(channel, name="first", topic="topic"),
                    self.queue.edit_channel(channel, name="second"),
                    self.queue.edit_channel(channel, topic="new topic")]
        self.assertTrue(all(await asyncio.gather(*versions)))
        channel.edit.assert_awaited_once_with(name="second", topic="new topic")

    async def test_reorders_coalesce_by_guild_and_group(self):
        old, newest, other_group, other_guild = (AsyncMock() for _ in range(4))
        futures = [self.queue.reorder(1, (20, "voice"), (3, 2), old),
                   self.queue.reorder(1, (20, "voice"), (2, 3), newest),
                   self.queue.reorder(1, (21, "voice"), (2, 3), other_group),
                   self.queue.reorder(2, (20, "voice"), (2, 3), other_guild)]
        self.assertTrue(all(await asyncio.gather(*futures)))
        old.assert_not_awaited()
        for operation in (newest, other_group, other_guild):
            operation.assert_awaited_once()

    async def test_channel_limit_holds_latest_without_blocking_other_destinations(self):
        channel, other = self.target(), self.target(11)
        now = [0.0]
        self.queue = PublishingQueue(clock=lambda: now[0])
        self.addAsyncCleanup(self.queue.close)
        await self.queue.edit_channel(channel, name="one")
        await self.queue.edit_channel(channel, topic="two")
        held = self.queue.edit_channel(channel, name="three")
        await asyncio.sleep(0)
        latest = self.queue.edit_channel(channel, name="four", topic="new topic")
        self.assertTrue(await self.queue.edit_channel(other, name="independent"))
        self.assertTrue(await self.queue.edit_message(self.target(12), content="board"))
        self.assertFalse(held.done())
        self.assertEqual(channel.edit.await_count, 2)
        now[0] = 600.0
        final = self.queue.edit_channel(channel, name="four")
        self.assertTrue(all(await asyncio.wait_for(asyncio.gather(held, latest, final), 1)))
        self.assertEqual(channel.edit.await_args.kwargs, {"name": "four", "topic": "new topic"})
        self.assertEqual(channel.edit.await_count, 3)

    async def test_identical_message_channel_and_order_skip_sends(self):
        message, channel, operation = self.target(), self.target(11), AsyncMock()
        for _ in range(2):
            self.assertTrue(await self.queue.edit_message(message, embed=discord.Embed(title="board")))
            self.assertTrue(await self.queue.edit_channel(channel, name="name", reason=str(_)))
            self.assertTrue(await self.queue.reorder(1, (20, "voice"), (2, 3), operation))
        message.edit.assert_awaited_once()
        channel.edit.assert_awaited_once()
        operation.assert_awaited_once()

    async def test_unchanged_channel_fields_do_not_use_another_name_slot(self):
        channel = self.target()
        await self.queue.edit_channel(channel, name="name", topic="topic")
        await self.queue.edit_channel(channel, name="name")
        self.assertEqual(channel.edit.await_count, 1)
        self.assertTrue(await self.queue.edit_channel(channel, name="other"))

    async def test_other_channel_fields_are_not_given_the_name_topic_limit(self):
        channel = self.target()
        for index in range(5):
            self.assertTrue(await asyncio.wait_for(
                self.queue.edit_channel(channel, archived=bool(index % 2)), 1,
            ))
        self.assertEqual(channel.edit.await_count, 5)

    async def test_changed_view_is_not_hidden_by_identical_object_identity(self):
        message = self.target()
        view = discord.ui.View()
        button = discord.ui.Button(label="Test", custom_id="test")
        view.add_item(button)
        await self.queue.edit_message(message, view=view)
        button.disabled = True
        await self.queue.edit_message(message, view=view)
        self.assertEqual(message.edit.await_count, 2)

    async def test_notfound_and_forbidden_drop_pending_and_future_versions(self):
        for index, kind, status in ((10, discord.NotFound, 404), (11, discord.Forbidden, 403)):
            with self.subTest(kind=kind):
                target = self.target(index)
                entered, finish = asyncio.Event(), asyncio.Event()
                error = http_error(kind, status)

                async def fail(**fields):
                    entered.set()
                    await finish.wait()
                    raise error

                target.edit.side_effect = fail
                first = self.queue.edit_message(target, content="first")
                await entered.wait()
                newer = self.queue.edit_message(target, content="second")
                with self.assertLogs("elbow_helper.discord.publishing", "WARNING") as logs:
                    finish.set()
                    results = await asyncio.gather(first, newer)
                    later = await self.queue.edit_message(target, content="third")
                self.assertEqual(len(logs.output), 1)
                self.assertFalse(any(results + [later]))
                self.assertTrue(all(result.error is error for result in results + [later]))
                target.edit.assert_awaited_once()

    async def test_failed_channel_is_independent_of_message_with_same_id(self):
        channel = self.target()
        channel.edit.side_effect = http_error(discord.NotFound, 404)
        with self.assertLogs("elbow_helper.discord.publishing", "WARNING"):
            self.assertFalse(await self.queue.edit_channel(channel, name="gone"))
        message = self.target()
        self.assertTrue(await self.queue.edit_message(message, content="still here"))

    async def test_http_retries_then_fails_without_raising_or_repeated_warnings(self):
        message = self.target()
        message.edit.side_effect = http_error()
        with patch("elbow_helper.infrastructure.discord.publishing.HTTP_RETRY_DELAYS", (0, 0)):
            with self.assertLogs("elbow_helper.discord.publishing", "WARNING") as logs:
                self.assertFalse(await self.queue.edit_message(message, content="one"))
                self.assertFalse(await self.queue.edit_message(message, content="two"))
        self.assertEqual(message.edit.await_count, 6)
        self.assertEqual(len(logs.output), 1)

    async def test_retry_sends_newest_pending_version_and_resolves_replaced_waiter(self):
        message = self.target()
        failed = asyncio.Event()

        async def edit(**fields):
            if message.edit.await_count == 1:
                failed.set()
                raise http_error()
            return fields["content"]

        message.edit.side_effect = edit
        with patch("elbow_helper.infrastructure.discord.publishing.HTTP_RETRY_DELAYS", (0.02, 0.02)):
            first = self.queue.edit_message(message, content="old")
            await failed.wait()
            newest = self.queue.edit_message(message, content="new")
            results = await asyncio.gather(first, newest)
        self.assertEqual([result.value for result in results], ["new", "new"])
        self.assertEqual([call.kwargs["content"] for call in message.edit.await_args_list], ["old", "new"])

    async def test_failed_operation_does_not_poison_next_submission(self):
        message = self.target()
        message.edit.side_effect = [ValueError("synthetic"), message]
        with self.assertLogs("elbow_helper.discord.publishing", "WARNING"):
            self.assertFalse(await self.queue.edit_message(message, content="bad"))
        self.assertTrue(await self.queue.edit_message(message, content="good"))

    async def test_incomplete_order_is_not_cached_as_applied(self):
        operation = AsyncMock(side_effect=[False, None])
        with self.assertLogs("elbow_helper.discord.publishing", "WARNING"):
            self.assertFalse(await self.queue.reorder(1, (20, "text"), (2, 3), operation))
        self.assertTrue(await self.queue.reorder(1, (20, "text"), (2, 3), operation))
        self.assertEqual(operation.await_count, 2)

    async def test_invalid_signature_resolves_failed_and_allows_a_later_version(self):
        class InvalidEmbed(discord.Embed):
            def to_dict(self):
                raise ValueError("synthetic invalid embed")

        message = self.target()
        with self.assertLogs("elbow_helper.discord.publishing", "WARNING"):
            self.assertFalse(await asyncio.wait_for(self.queue.edit_message(message, embed=InvalidEmbed()), 1))
        self.assertTrue(await self.queue.edit_message(message, content="valid"))

    async def test_new_submission_during_previous_worker_cleanup_is_not_stranded(self):
        class InvalidEmbed(discord.Embed):
            def to_dict(self):
                raise ValueError("synthetic invalid embed")

        entered, finish = asyncio.Event(), asyncio.Event()
        dispose = self.queue._dispose_files
        calls = 0

        async def delayed_dispose():
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                await finish.wait()
            await dispose()

        message = self.target()
        with patch.object(self.queue, "_dispose_files", side_effect=delayed_dispose):
            with self.assertLogs("elbow_helper.discord.publishing", "WARNING"):
                self.assertFalse(await self.queue.edit_message(message, embed=InvalidEmbed()))
            await entered.wait()
            try:
                self.assertTrue(await asyncio.wait_for(
                    self.queue.edit_message(message, content="valid"), 1,
                ))
            finally:
                finish.set()

    async def test_interaction_and_original_response_edits_bypass_the_queue(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock()),
            edit_original_response=AsyncMock(), original_response=AsyncMock(),
        )
        view = discord.ui.View()
        await edit_bound_view(interaction, view=view, content="test")
        await edit_original_bound_view(interaction, view=view, content="test")
        interaction.response.edit_message.assert_awaited_once_with(view=view, content="test")
        interaction.edit_original_response.assert_awaited_once_with(view=view, content="test")
        self.assertEqual(self.queue._destinations, {})

    async def test_image_uploads_are_read_off_loop_recreated_for_retry_and_deduplicated(self):
        message = self.target()
        read_threads, bodies = [], []

        class Buffer(BytesIO):
            def read(self, *args):
                read_threads.append(threading.get_ident())
                return super().read(*args)

        async def edit(**fields):
            upload = fields["attachments"][0]
            bodies.append(upload.fp.read())
            upload.close()
            if len(bodies) == 1:
                raise http_error()
            return message

        message.edit.side_effect = edit
        with patch("elbow_helper.infrastructure.discord.publishing.HTTP_RETRY_DELAYS", (0, 0)):
            for _ in range(2):
                upload = discord.File(Buffer(b"image"), filename="image.png")
                self.assertTrue(await self.queue.edit_message(message, attachments=[upload]))
        self.assertEqual(bodies, [b"image", b"image"])
        self.assertTrue(all(thread != threading.get_ident() for thread in read_threads))

    async def test_replaced_upload_is_released_without_reading_or_sending_it(self):
        message = self.target()
        old = discord.File(BytesIO(b"old"), filename="old.png")
        with patch.object(discord.File, "close", autospec=True) as close:
            first = self.queue.edit_message(message, attachments=[old])
            newest = self.queue.edit_message(message, content="new", attachments=[])
            self.assertTrue(all(await asyncio.gather(first, newest)))
            close.assert_called_once_with(old)
        message.edit.assert_awaited_once_with(content="new", attachments=[])
        old.close()

    async def test_upload_read_failure_resolves_replaced_waiters_without_sending(self):
        class BrokenBuffer(BytesIO):
            def read(self, *args):
                raise OSError("synthetic read failure")

        message = self.target()
        first = self.queue.edit_message(message, content="old")
        latest = self.queue.edit_message(message, attachments=[discord.File(BrokenBuffer(b"x"))])
        with self.assertLogs("elbow_helper.discord.publishing", "WARNING"):
            results = await asyncio.wait_for(asyncio.gather(first, latest), 1)
        self.assertFalse(any(results))
        self.assertTrue(all(isinstance(result.error, OSError) for result in results))
        message.edit.assert_not_awaited()

    async def test_shutdown_flushes_pending_newest_and_rejects_later_submissions(self):
        message = self.target()
        old = self.queue.edit_message(message, content="old")
        newest = self.queue.edit_message(message, content="new")
        await self.queue.close()
        self.assertTrue(await old)
        self.assertTrue(await newest)
        message.edit.assert_awaited_once_with(content="new")
        self.assertFalse(await self.queue.edit_message(message, content="too late"))
        await self.queue.close()

    async def test_shutdown_bounds_inflight_and_pending_work_and_logs_once(self):
        message = self.target()
        entered = asyncio.Event()

        async def slow(**fields):
            entered.set()
            await asyncio.Event().wait()

        message.edit.side_effect = slow
        active = self.queue.edit_message(message, content="active")
        await entered.wait()
        pending = self.queue.edit_message(message, content="pending")
        with patch("elbow_helper.infrastructure.discord.publishing.SHUTDOWN_TIMEOUT", 0.01):
            with self.assertLogs("elbow_helper.discord.publishing", "WARNING") as logs:
                await asyncio.wait_for(self.queue.close(), 1)
        self.assertFalse(await active)
        self.assertFalse(await pending)
        self.assertEqual(len(logs.output), 1)

    async def test_shutdown_drops_a_channel_held_by_its_window(self):
        channel = self.target()
        await self.queue.edit_channel(channel, name="first")
        await self.queue.edit_channel(channel, topic="second")
        held = self.queue.edit_channel(channel, name="held")
        with patch("elbow_helper.infrastructure.discord.publishing.SHUTDOWN_TIMEOUT", 0.01):
            with self.assertLogs("elbow_helper.discord.publishing", "WARNING") as logs:
                await self.queue.close()
        self.assertFalse(await held)
        self.assertEqual(channel.edit.await_count, 2)
        self.assertEqual(len(logs.output), 1)


if __name__ == "__main__":
    unittest.main()
