"""Synthetic Discord permission equivalence and directory scaling regressions."""

import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.channels import SUPPORT_TICKET_CATEGORY
from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID
from elbow_helper.agent import disclosure
from elbow_helper.agent.access import AgentAccessLost, require_evidence_access
from elbow_helper.agent.models import AgentRequestContext
from elbow_helper.agent.research.directory import scoped_channel_directory


def _overwrite(identifier, *, member=False, **permissions):
    allow, deny = discord.PermissionOverwrite(**permissions).pair()
    return {"id": str(identifier), "type": int(member),
            "allow": str(allow.value), "deny": str(deny.value)}


class SyntheticGuild:
    def __init__(self, members=12, channels=0):
        self.state = discord.Client(intents=discord.Intents.none())._connection
        self.state.user = SimpleNamespace(id=9999)
        roles = [(1, discord.Permissions(view_channel=True, read_message_history=True)),
                 (AGENT_TESTER_ROLE_ID, discord.Permissions.none()),
                 (5, discord.Permissions.none()),
                 (6, discord.Permissions(manage_threads=True)),
                 (7, discord.Permissions(administrator=True))]
        self.guild = discord.Guild(state=self.state, data={
            "id": "1", "name": "Synthetic", "owner_id": "10", "member_count": members,
            "roles": [{"id": str(identifier), "name": str(identifier),
                       "permissions": str(permissions.value), "position": index}
                      for index, (identifier, permissions) in enumerate(roles)],
        })
        for index in range(members - 1):
            self.add_member(10 + index, [AGENT_TESTER_ROLE_ID, (5, 6, 7)[index % 3]])
        self.add_member(9999, [7], bot=True)
        self.destination = self.channel(100)
        self.sources = [self.channel(200 + index) for index in range(channels)]

    def add_member(self, identifier, roles, *, bot=False):
        member = discord.Member(guild=self.guild, state=self.state, data={
            "user": {"id": str(identifier), "username": str(identifier),
                     "discriminator": "0", "avatar": None, "bot": bot},
            "roles": list(map(str, roles)), "flags": 0,
        })
        self.guild._add_member(member)
        return member

    def channel(self, identifier, overwrites=(), *, forum=False):
        cls = discord.ForumChannel if forum else discord.TextChannel
        channel = cls(guild=self.guild, state=self.state, data={
            "id": str(identifier), "name": str(identifier), "type": 15 if forum else 0,
            "position": identifier, "parent_id": str(SUPPORT_TICKET_CATEGORY),
            "permission_overwrites": list(overwrites),
        })
        self.guild._add_channel(channel)
        return channel

    def thread(self, identifier, parent, *, private=False):
        return discord.Thread(guild=self.guild, state=self.state, data={
            "id": str(identifier), "parent_id": str(parent.id), "owner_id": "10",
            "name": str(identifier), "type": 12 if private else 11,
            "message_count": 0, "member_count": 0,
            "thread_metadata": {"archived": False, "auto_archive_duration": 1440,
                                "archive_timestamp": "2026-01-01T00:00:00+00:00"},
        })

    def context(self, destination=None):
        return AgentRequestContext(
            bot=SimpleNamespace(), guild=self.guild, member=self.guild.get_member(10),
            source_message=SimpleNamespace(channel=self.destination),
            delivery_channel=destination, account_links=None, message_search=None,
        )


async def _legacy_humans(destination, guild, cache):
    """Original uncached viewer resolution, independent of the new grouping."""
    if not guild.chunked:
        return None
    if isinstance(destination, discord.Thread):
        if destination.is_private():
            identifiers = await disclosure._thread_member_ids(destination, cache)
            if identifiers is None:
                return None
            members = tuple(guild.get_member(identifier) for identifier in identifiers)
            if any(member is None for member in members):
                return None
            moderators = tuple(member for member in guild.members
                               if (permissions := destination.permissions_for(member)).view_channel
                               and permissions.manage_threads)
            viewers = (*members, *moderators)
        else:
            viewers = tuple(member for member in guild.members
                            if destination.parent.permissions_for(member).view_channel)
    else:
        viewers = tuple(member for member in guild.members
                        if destination.permissions_for(member).view_channel)
    return tuple(member for member in viewers if not member.bot and member.id != guild.me.id)


async def _legacy_readers(source, destination, guild, humans, identifiers, cache):
    return all((permissions := source.permissions_for(member)).view_channel
               and permissions.read_message_history
               and (identifiers is None or member.id in identifiers or permissions.manage_threads)
               for member in humans)


@contextmanager
def _legacy_scans():
    with patch.object(disclosure, "_destination_humans", _legacy_humans), \
            patch.object(disclosure, "_source_readable_by_viewers", _legacy_readers):
        yield


@contextmanager
def _permission_counts():
    counts = Counter()
    original = discord.abc.GuildChannel.permissions_for

    def counted(channel, actor):
        counts[channel.id] += 1
        return original(channel, actor)

    with patch.object(discord.abc.GuildChannel, "permissions_for", counted):
        yield counts


class DirectoryPermissionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_decisions_match_original_member_scan(self):
        fixture = SyntheticGuild()
        guild = fixture.guild
        # Include a timeout, a guild owner, administrators and overlapping roles.
        guild.get_member(13).timed_out_until = datetime.now(timezone.utc) + timedelta(days=1)
        guild.get_member(11)._roles = discord.utils.SnowflakeList([AGENT_TESTER_ROLE_ID, 5, 6])
        # Discord can retain a role ID on a member before the role is cached.
        guild.get_member(19)._roles.add(888)
        sources = [
            fixture.channel(200),
            fixture.channel(201, [_overwrite(14, member=True, view_channel=False)]),
            fixture.channel(202, [_overwrite(14, member=True, read_message_history=False)]),
            fixture.channel(203, [_overwrite(10, member=True, view_channel=False),
                                  _overwrite(12, member=True, read_message_history=False)]),
            fixture.channel(204, [_overwrite(5, read_message_history=False),
                                  _overwrite(6, read_message_history=True)]),
            fixture.channel(205, [_overwrite(1, view_channel=False),
                                  _overwrite(5, view_channel=True),
                                  _overwrite(14, member=True, view_channel=True)]),
            fixture.channel(206, [_overwrite(13, member=True, manage_threads=True)]),
            fixture.channel(207, [_overwrite(14, member=True, read_message_history=False)], forum=True),
            fixture.channel(208, [_overwrite(888, read_message_history=False)]),
        ]
        sources.append(fixture.thread(300, sources[2]))
        self.assertFalse(await disclosure.personal_source_allowed(fixture.destination, sources[8], guild))
        private = fixture.thread(301, sources[6], private=True)
        sources.append(private)
        destinations = [fixture.destination,
                        fixture.channel(101, [_overwrite(14, member=True, view_channel=False)]),
                        fixture.thread(102, fixture.destination),
                        fixture.thread(103, fixture.destination, private=True)]
        results = []
        for chunked in (True, False):
            guild._member_count = len(guild.members) if chunked else len(guild.members) + 1
            for destination in destinations:
                for joined in ([10, 11, 12], list(range(10, 21)), [404], None):
                    async def fetch_members(thread):
                        if joined is None:
                            raise OSError("Synthetic fetch failure")
                        return [SimpleNamespace(id=identifier) for identifier in joined]

                    with patch.object(discord.Thread, "fetch_members", fetch_members):
                        with _legacy_scans():
                            expected = [await disclosure.personal_source_allowed(
                                destination, source, guild, thread_members={},
                            ) for source in sources]
                            clearance = await disclosure.destination_clearance(destination, guild)
                        cache = {}
                        actual = [await disclosure.personal_source_allowed(
                            destination, source, guild, thread_members=cache,
                        ) for source in sources]
                        self.assertEqual(actual, expected)
                        self.assertEqual(await disclosure.destination_clearance(
                            destination, guild, thread_members=cache), clearance)
                        results.extend(actual)
        self.assertIn(True, results)
        self.assertIn(False, results)

    async def test_recheck_recomputes_viewers_and_denies_new_reader(self):
        fixture = SyntheticGuild()
        outsider = 14
        destination = fixture.destination = fixture.channel(
            100, [_overwrite(outsider, member=True, view_channel=False)],
        )
        source = fixture.channel(200, [_overwrite(outsider, member=True, read_message_history=False)])
        context = fixture.context()
        self.assertIn(str(source.id), json.loads(await scoped_channel_directory(context)))
        context.state.source_channels.add(source.id)
        destination._fill_overwrites({"permission_overwrites": []})
        with _permission_counts() as counts:
            with self.assertRaises(AgentAccessLost):
                await require_evidence_access(context)
        self.assertGreaterEqual(counts[destination.id], len(fixture.guild.members))
        self.assertNotIn(str(source.id), json.loads(await scoped_channel_directory(context)))
        # A fresh request never inherits the previous request's viewer snapshot.
        self.assertNotIn(str(source.id), json.loads(await scoped_channel_directory(fixture.context())))

    async def test_private_destination_scans_moderators_once_until_recheck(self):
        fixture = SyntheticGuild(channels=20)
        destination = fixture.thread(400, fixture.destination, private=True)
        context = fixture.context(destination)
        fetch = AsyncMock(return_value=[fixture.guild.get_member(10)])
        with patch.object(discord.Thread, "fetch_members", fetch), _permission_counts() as counts:
            first = await scoped_channel_directory(context)
            parent_checks = counts[fixture.destination.id]
            self.assertEqual(await scoped_channel_directory(context), first)
            self.assertEqual(counts[fixture.destination.id] - parent_checks,
                             parent_checks - len(fixture.guild.members))
            fetch.assert_awaited_once()
            # New moderators are viewers even when they have not joined.
            source = fixture.sources[0]
            source._fill_overwrites({"permission_overwrites": [
                _overwrite(13, member=True, read_message_history=False),
            ]})
            context.state.source_channels.add(source.id)
            fixture.destination._fill_overwrites({"permission_overwrites": [
                _overwrite(13, member=True, manage_threads=True),
            ]})
            with self.assertRaises(AgentAccessLost):
                await require_evidence_access(context)
            self.assertEqual(fetch.await_count, 2)

    async def test_large_directory_permission_counts_and_repeated_pass(self):
        fixture = SyntheticGuild(members=2000, channels=300)
        with _legacy_scans(), _permission_counts() as before:
            expected = await scoped_channel_directory(fixture.context())
        self.assertGreater(sum(before.values()), 1_000_000)
        context = fixture.context()
        yielded = []
        with _permission_counts() as after:
            asyncio.get_running_loop().call_soon(lambda: yielded.append(sum(after.values())))
            self.assertEqual(await scoped_channel_directory(context), expected)
            first_checks = sum(after.values())
            self.assertEqual(after[fixture.destination.id], 2001)
            self.assertLess(first_checks, 10_000)
            self.assertEqual(await scoped_channel_directory(context), expected)
            self.assertEqual(after[fixture.destination.id], 2002)
            self.assertLess(sum(after.values()) - first_checks, 5_000)
        self.assertTrue(yielded)
        self.assertLess(yielded[0], first_checks)
