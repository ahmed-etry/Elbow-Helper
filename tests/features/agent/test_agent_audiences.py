"""Source disclosure follows actual destination membership and read permissions."""

from dataclasses import dataclass
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.roles import LEAD, LEAD_PLUS
from elbow_helper.features.agent.access import ACCESS_LEAD, ACCESS_LEAD_PLUS
from elbow_helper.features.agent.disclosure import can_show


@dataclass(frozen=True)
class Member:
    id: int
    roles: tuple
    bot: bool = False


@dataclass(frozen=True)
class Role:
    id: int


class Channel:
    type = discord.ChannelType.text

    def __init__(self, identifier, guild, viewers):
        self.id = identifier
        self.guild = guild
        self.viewers = set(viewers)
        self.history_denied = set()
        self.overwrites = {Role(identifier): SimpleNamespace(
            view_channel=True, read_message_history=True,
        )}

    def permissions_for(self, member):
        visible = member.id in self.viewers
        return SimpleNamespace(
            view_channel=visible,
            read_message_history=visible and member.id not in self.history_denied,
        )


class Thread:
    def __init__(self, identifier, parent, *, private=False, members=()):
        self.id = identifier
        self.parent = parent
        self.guild = parent.guild
        self.private = private
        self.fetch_members = AsyncMock(return_value=list(members))

    def is_private(self):
        return self.private

    def permissions_for(self, member):
        return self.parent.permissions_for(member)


class AudienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        lead = Role(next(iter(LEAD)))
        lead_plus = Role(next(iter(LEAD_PLUS - LEAD)))
        self.members = [Member(2, (lead,)), Member(3, (lead_plus,)), Member(99, (lead,))]
        self.guild = SimpleNamespace(
            id=1, chunked=True, members=self.members, me=self.members[-1],
            get_member=lambda identifier: next(
                (member for member in self.members if member.id == identifier), None,
            ),
            default_role=Role(1), roles=[Role(1), lead, lead_plus],
        )
        self.source = Channel(20, self.guild, {2, 3, 99})
        self.destination = Channel(10, self.guild, {2, 99})
        self.thread_type = patch(
            "elbow_helper.features.agent.disclosure.discord.Thread", Thread,
        )
        self.thread_type.start()
        self.addCleanup(self.thread_type.stop)

    async def test_narrower_destination_is_allowed_and_wider_destination_is_denied(self):
        self.assertTrue(await can_show(self.destination, {20: self.source}, set(), self.guild))
        self.source.viewers.remove(3)
        self.destination.viewers.add(3)
        self.assertFalse(await can_show(self.destination, {20: self.source}, set(), self.guild))

    async def test_source_member_history_deny_and_bot_deny_are_enforced(self):
        for identifier in (2, 99):
            with self.subTest(identifier=identifier):
                self.source.history_denied = {identifier}
                self.assertFalse(await can_show(
                    self.destination, {20: self.source}, set(), self.guild,
                ))
                self.source.history_denied.clear()
                self.source.viewers.remove(identifier)
                self.assertFalse(await can_show(
                    self.destination, {20: self.source}, set(), self.guild,
                ))
                self.source.viewers.add(identifier)

    async def test_same_channel_keeps_its_existing_disclosure_rule(self):
        self.destination.history_denied.add(2)
        self.assertTrue(await can_show(
            self.destination, {10: self.destination}, set(), self.guild,
        ))

    async def test_unchunked_guild_uses_strict_overwrite_comparison(self):
        self.guild.chunked = False
        self.assertFalse(await can_show(self.destination, {20: self.source}, set(), self.guild))
        self.source.overwrites = self.destination.overwrites
        self.source.viewers = self.destination.viewers.copy()
        self.assertTrue(await can_show(self.destination, {20: self.source}, set(), self.guild))

    async def test_public_thread_uses_all_parent_viewers(self):
        destination = Thread(11, self.destination)
        self.assertTrue(await can_show(destination, {20: self.source}, set(), self.guild))
        self.source.viewers.remove(3)
        self.destination.viewers.add(3)
        self.assertFalse(await can_show(destination, {20: self.source}, set(), self.guild))
        destination.fetch_members.assert_not_awaited()

    async def test_private_thread_members_are_cached_but_permissions_are_rechecked(self):
        destination = Thread(11, self.source, private=True, members=[self.members[0]])
        cache = {}
        for _ in range(2):
            self.assertTrue(await can_show(
                destination, {20: self.source}, set(), self.guild, thread_members=cache,
            ))
        destination.fetch_members.assert_awaited_once()
        self.source.history_denied.add(2)
        self.assertFalse(await can_show(
            destination, {20: self.source}, set(), self.guild, thread_members=cache,
        ))

    async def test_private_source_requires_every_viewer_to_be_a_thread_member(self):
        source = Thread(21, self.source, private=True, members=self.members[1:])
        self.assertFalse(await can_show(self.destination, {21: source}, set(), self.guild))
        source.fetch_members.return_value = self.members
        self.assertTrue(await can_show(self.destination, {21: source}, set(), self.guild))

    async def test_failed_private_thread_fetch_uses_the_strict_rule(self):
        destination = Thread(11, self.source, private=True)
        destination.fetch_members.side_effect = OSError("Unavailable")
        self.assertFalse(await can_show(destination, {20: self.source}, set(), self.guild))

    async def test_bots_are_ignored_for_access_levels_and_source_readership(self):
        self.members[-1] = Member(99, (), bot=True)
        self.guild.me = self.members[-1]
        self.members.append(Member(98, (), bot=True))
        self.destination.viewers.add(98)
        self.assertTrue(await can_show(
            self.destination, {20: self.source}, {ACCESS_LEAD, ACCESS_LEAD_PLUS}, self.guild,
        ))

    async def test_one_human_without_any_required_level_fails(self):
        self.destination.viewers.add(3)
        self.assertTrue(await can_show(
            self.destination, {20: self.source}, {ACCESS_LEAD_PLUS}, self.guild,
        ))
        self.assertFalse(await can_show(
            self.destination, {20: self.source}, {ACCESS_LEAD, ACCESS_LEAD_PLUS}, self.guild,
        ))
