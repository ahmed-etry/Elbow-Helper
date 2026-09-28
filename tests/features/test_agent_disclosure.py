from dataclasses import dataclass
from types import SimpleNamespace
import unittest
import discord
from elbow_helper.configuration.roles import CORE, LEAD
from elbow_helper.features.agent.access import ACCESS_LEAD, can_disclose_provenance

@dataclass(frozen=True)
class _Role:
    id: int

@dataclass(frozen=True)
class _Member:
    id: int
    roles: tuple[_Role, ...]

    @property
    def display_name(self):
        return "Test member"

class _Channel:
    type = discord.ChannelType.text

    def __init__(self, channel_id, guild, *, public=False, allow=(), overwrites=None):
        self.id = channel_id
        self.guild = guild
        self.public = public
        self.allow = frozenset(allow)
        self.overwrites = overwrites if overwrites is not None else {}

    def permissions_for(self, actor):
        role_ids = {role.id for role in getattr(actor, "roles", ())}
        role_ids.add(actor.id)
        visible = actor.id == 99 or self.public or bool(role_ids & self.allow)
        return SimpleNamespace(view_channel=visible, read_message_history=visible)


class AgentDisclosureTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_fallback_skips_roles_managed_by_bots(self):
        destination = self.channel(100, allow={self.lead.id, 77}, overwrites={})
        self.context.source_message.channel = destination
        managed = SimpleNamespace(id=77, managed=True)
        self.guild.roles.append(managed)
        self.assertTrue(await can_disclose_provenance(self.context, {100}, {ACCESS_LEAD}))

    def setUp(self):
        self.default = _Role(1)
        self.lead = _Role(next(iter(LEAD)))
        self.core = _Role(next(iter(CORE - LEAD)))
        self.member = _Member(42, (self.lead, self.core))
        self.bot_member = _Member(99, ())
        self.guild = SimpleNamespace(
            id=1, name="Brown Elbow", default_role=self.default,
            roles=[self.default, self.lead, self.core],
            me=self.bot_member,
            get_member=lambda member_id: self.member if member_id == 42 else None,
        )
        self.channels = {}
        self.guild.get_channel_or_thread = self.channels.get
        self.context = SimpleNamespace(
            guild=self.guild, member=self.member,
            bot=SimpleNamespace(), source_message=SimpleNamespace(channel=None),
        )

    def channel(self, channel_id, **kwargs):
        channel = _Channel(channel_id, self.guild, **kwargs)
        self.channels[channel_id] = channel
        return channel
