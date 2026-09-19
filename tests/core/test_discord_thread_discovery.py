from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import discord

from elbow_helper.discord.thread_discovery import (
    DiscordThreadDiscovery, DiscordThreadDiscoveryError,
)


def _thread(thread_id, *, parent_id=10, private=False, archived=True):
    return SimpleNamespace(
        id=thread_id, parent_id=parent_id, archived=archived,
        archive_timestamp=datetime(2026, 9, 18, 12, thread_id % 60,
                                   tzinfo=timezone.utc),
        is_private=lambda: private,
    )


def _parent(threads):
    parent = Mock(spec=discord.TextChannel)
    parent.id = 10

    async def archived_threads(**kwargs):
        parent.arguments = kwargs
        for thread in threads:
            yield thread

    parent.archived_threads = archived_threads
    return parent


class DiscordThreadDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_archive_page_uses_bounded_before_timestamp(self):
        threads = (_thread(30), _thread(20))
        parent = _parent(threads)
        page = await DiscordThreadDiscovery().archived_page(
            parent, private=False, joined=False, before_id=100, limit=3,
        )
        self.assertEqual(page.threads, threads)
        self.assertTrue(page.endpoint_exhausted)
        self.assertIsNone(page.next_before_id)
        self.assertEqual(page.mode, "public")
        self.assertIsInstance(parent.arguments["before"], datetime)
        self.assertEqual(parent.arguments["limit"], 3)
        self.assertFalse(parent.arguments["private"])

    async def test_joined_private_page_uses_thread_id_cursor(self):
        threads = (
            _thread(30, private=True), _thread(20, private=True),
        )
        parent = _parent(threads)
        page = await DiscordThreadDiscovery().archived_page(
            parent, private=True, joined=True, before_id=40, limit=2,
        )
        self.assertFalse(page.endpoint_exhausted)
        self.assertEqual(page.next_before_id, 20)
        self.assertEqual(page.mode, "private_joined")
        self.assertEqual(parent.arguments["before"].id, 40)
        self.assertTrue(parent.arguments["private"])
        self.assertTrue(parent.arguments["joined"])

    async def test_out_of_scope_archive_response_fails_closed(self):
        parent = _parent((_thread(30, parent_id=11),))
        with self.assertRaises(DiscordThreadDiscoveryError):
            await DiscordThreadDiscovery().archived_page(
                parent, private=False, joined=False,
                before_id=None, limit=3,
            )
