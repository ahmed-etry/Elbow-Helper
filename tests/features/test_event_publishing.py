from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from elbow_helper.infrastructure.discord.publishing import PublishingQueue
from elbow_helper.features.event_stats.channels import EventStatsChannelsMixin


class EventPublishingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = [0.0]
        self.queue = PublishingQueue(clock=lambda: self.now[0])
        self.addAsyncCleanup(self.queue.close)
        self.owner = EventStatsChannelsMixin()
        self.owner.bot = SimpleNamespace(publishing=self.queue)
        self.channel = MagicMock(spec=discord.VoiceChannel)
        self.channel.id = 10
        self.channel.category_id = None
        self.channel.name = "old"
        self.channel.position = 0
        self.channel.edit = AsyncMock(return_value=self.channel)
        self.channel.move = AsyncMock()
        self.guild = SimpleNamespace(id=1, channels=[self.channel],
                                     get_channel=lambda id: self.channel if id == 10 else None)
        self.owner._resolve_category = lambda guild, event: None
        self.owner.events = [{"key": "members", "name": "Members", "type": "counter",
                              "channel_id": 10, "position": 0}]
        self.count = 0
        self.owner._counter_value = lambda guild, event: self.count

    async def test_refresh_keeps_submitting_newest_name_while_window_is_full(self):
        await self.queue.edit_channel(self.channel, name="one")
        await self.queue.edit_channel(self.channel, topic="two")
        for count in range(5):
            self.count = count
            self.assertFalse(await asyncio.wait_for(self.owner.refresh_all_channels(self.guild), 1))
        self.assertEqual(self.channel.edit.await_count, 2)
        self.now[0] = 600.0
        await self.owner.refresh_all_channels(self.guild)
        await self.queue.close()
        self.channel.edit.assert_awaited_with(name="Members: 4", reason="Update event statistics")
        self.assertEqual(self.channel.edit.await_count, 3)

    async def test_no_change_check_replaces_an_obsolete_waiting_name(self):
        await self.queue.edit_channel(self.channel, name="Members: 9")
        await self.queue.edit_channel(self.channel, topic="topic")
        pending = self.queue.edit_channel(self.channel, name="obsolete")
        await asyncio.sleep(0)
        self.channel.name = "Members: 9"
        self.count = 9
        await self.owner.refresh_all_channels(self.guild)
        self.assertTrue(await asyncio.wait_for(pending, 1))
        self.assertEqual(self.channel.edit.await_count, 2)

    async def test_event_order_submissions_send_only_the_latest_complete_order(self):
        other = MagicMock(spec=discord.VoiceChannel)
        other.id, other.position, other.category_id = 11, 1, None
        other.move = AsyncMock()
        self.guild.channels.append(other)
        for index in range(5):
            channels = [other, self.channel] if index % 2 == 0 else [self.channel, other]
            entries = [((0, position, 0, ""), channel, {}) for position, channel in enumerate(channels)]
            await self.owner._reorder_channel_group(self.guild, None, entries)
        await self.queue.close()
        other.move.assert_awaited_once_with(beginning=True, offset=0, category=None,
                                          reason="Reorder event trackers")
        self.channel.move.assert_awaited_once_with(after=other, category=None,
                                                 reason="Reorder event trackers")

    async def test_deleted_event_channel_order_fails_once_without_breaking_refresh(self):
        self.channel.category_id = 20
        self.channel.move.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="synthetic"), "deleted",
        )
        entries = [((0, 0, 0, ""), self.channel, {})]
        with self.assertLogs("elbow_helper.discord.publishing", "WARNING") as logs:
            await self.owner._reorder_channel_group(self.guild, None, entries)
            await asyncio.sleep(0)
            await self.owner._reorder_channel_group(self.guild, None, entries)
            await self.queue.close()
        self.assertEqual(self.channel.move.await_count, 1)
        self.assertEqual(len(logs.output), 1)


if __name__ == "__main__":
    unittest.main()
