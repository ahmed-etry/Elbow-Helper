"""Private checks recognize Manage Threads while preserving read gates."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from elbow_helper.features.agent.disclosure import can_show_in_dm
from features.agent import test_agent_audiences as fixtures


class PrivateThreadCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = fixtures.AudienceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.thread = fixtures.Thread(21, fixture.source, private=True)
        self.thread.fetch_member = AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Not a thread member",
        ))

    async def allowed(self, levels=frozenset()):
        return await can_show_in_dm(self.fixture.guild, 2, {21: self.thread}, levels)

    async def test_manage_threads_needs_no_membership_fetch(self):
        self.fixture.source.moderators.add(2)
        self.assertTrue(await self.allowed())
        self.thread.fetch_member.assert_not_awaited()

    async def test_ordinary_reader_needs_explicit_membership(self):
        self.assertFalse(await self.allowed())
        self.thread.fetch_member.assert_awaited_once_with(2)
        self.thread.fetch_member.side_effect = None
        self.thread.fetch_member.return_value = SimpleNamespace(id=2)
        self.assertTrue(await self.allowed())

    async def test_manage_threads_preserves_member_and_bot_reads_and_role_requirements(self):
        self.fixture.source.moderators.add(2)
        for actor_id in (2, 99):
            self.fixture.source.history_denied.add(actor_id)
            self.assertFalse(await self.allowed())
            self.fixture.source.history_denied.clear()
            self.fixture.source.viewers.remove(actor_id)
            self.assertFalse(await self.allowed())
            self.fixture.source.viewers.add(actor_id)
        self.assertFalse(await self.allowed({"core"}))
        self.thread.fetch_member.assert_not_awaited()
