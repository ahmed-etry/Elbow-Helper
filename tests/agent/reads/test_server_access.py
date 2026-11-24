"""Which channels can one role see that another cannot, across the server."""
import json
from types import SimpleNamespace
import unittest

import discord

from elbow_helper.agent.reads import access as access_reads
from tests.agent.request_fixtures import flow_context as _context


class ServerAccessTests(unittest.IsolatedAsyncioTestCase):

    async def test_role_comparison_covers_every_channel_the_asker_can_read(self):
        context = _context()
        guild, asker, room = context.guild, context.member, context.source_message.channel
        trial, member = SimpleNamespace(id=7, name='Trial'), SimpleNamespace(id=8, name='Member')
        for role in (trial, member):
            role.permissions = discord.Permissions.none()
        for role in guild.roles:
            role.name = f'role {role.id}'
        guild.roles = [*guild.roles, trial, member]

        def channel(identifier, name, viewers):
            def permissions_for(actor):
                seen = bool({actor.id, *(role.id for role in getattr(actor, 'roles', ()))} & viewers)
                return discord.Permissions(view_channel=seen, read_message_history=seen)
            return SimpleNamespace(id=identifier, name=name, type=discord.ChannelType.text, overwrites={},
                                   category_id=None, guild=guild, permissions_for=permissions_for)

        bot = guild.me.id
        welcome = channel(92, 'trial-welcome', {trial.id, asker.id, bot})
        chat = channel(93, 'member-chat', {member.id, asker.id, bot})
        lounge = channel(94, 'lounge', {trial.id, member.id, asker.id, bot})
        hidden = channel(95, 'hidden-staff', {trial.id, bot})  # the asker cannot read it
        room.name, room.permissions_for = 'co-lead-botting', channel(91, '', {asker.id, bot}).permissions_for
        guild.channels = (room, welcome, chat, lounge, hidden)
        channels = {item.id: item for item in guild.channels}
        guild.get_channel_or_thread = channels.get

        result = await access_reads.discord_access(
            context, {'role_names': ['Trial', 'Member'], 'all_channels': True})

        self.assertEqual([actor['actor_name'] for actor in result['actors']], ['Trial', 'Member'])
        self.assertEqual([row['channel_id'] for row in result['rows']], [91, 92, 93, 94])
        self.assertEqual(result['differing_channel_ids'], [92, 93])
        self.assertEqual(result['rows'][1]['view_channel'], [True, False])
        self.assertEqual(result['channels_left_out'], 1)
        self.assertNotIn('hidden-staff', json.dumps(result))
        self.assertNotIn(95, context.state.source_channels)
        self.assertLessEqual({91, 92, 93, 94}, context.state.source_channels)
