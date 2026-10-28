"""Member-role grants cannot erase narrower Discord read denials."""

from dataclasses import replace
import unittest

import discord

from elbow_helper.configuration import channels, roles
from elbow_helper.agent.access import accessible_message_channel
from elbow_helper.agent.disclosure import source_access_levels
from elbow_helper.agent.models import AgentTurnState
from agent import test_role_category_scope as fixtures


class MemberReadDenialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = fixtures.RoleCategoryScopeTests()
        fixture.setUp()
        self.fixture = fixture
        self.context = replace(fixture.context, private_pass=True)

    async def test_member_shortcut_respects_member_and_role_denials(self):
        for permission in ("view_channel", "read_message_history"):
            for target in (self.context.guild.get_member(10), self.context.guild.get_role(self.fixture.recruiter)):
                with self.subTest(permission=permission, target=target.id):
                    source = self.fixture.channel(600, 900, grants=[roles.MEMBER_ROLE_ID, 77], member_grant=10)
                    overwrites = source.overwrites
                    overwrites[target] = discord.PermissionOverwrite(**{permission: False})
                    source._fill_overwrites({"permission_overwrites": [
                        {"id": str(actor.id), "type": 0 if isinstance(actor, discord.Role) else 1,
                         "allow": str(overwrite.pair()[0].value), "deny": str(overwrite.pair()[1].value)}
                        for actor, overwrite in overwrites.items()
                    ]})
                    self.context = replace(self.context, state=AgentTurnState())
                    self.assertIsNone(source_access_levels(source, self.context.guild))
                    if not getattr(source.permissions_for(self.context.member), permission):
                        self.assertIsNone(await accessible_message_channel(self.context, source.id))

    async def test_ordinary_and_configured_member_sources_keep_existing_class(self):
        source = self.fixture.channel(601, 900, grants=[roles.MEMBER_ROLE_ID, 77])
        self.assertEqual(source_access_levels(source, self.context.guild), set())
        self.assertIs(await accessible_message_channel(self.context, source.id), source)
        configured = self.fixture.channel(next(iter(channels.CLAN_CHAT_CHANNELS.values())), 900,
                                          grants=[roles.MEMBER_ROLE_ID, 77], deny=11)
        self.assertEqual(source_access_levels(configured, self.context.guild), set())
