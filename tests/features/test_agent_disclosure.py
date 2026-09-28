from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.roles import CORE, LEAD
from elbow_helper.features.agent.access import (
    ACCESS_LEAD, can_disclose_provenance,
)
from elbow_helper.features.agent.models import AgentRequestContext, RegisteredAgentTool
from elbow_helper.features.agent.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentToolDefinition, AgentUsage
from tests.features.agent_plan_helpers import plan_call


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

    async def test_same_channel_and_public_source_can_be_disclosed(self):
        destination = self.channel(100, public=True)
        source = self.channel(200, public=True)
        self.context.source_message.channel = destination

        self.assertTrue(await can_disclose_provenance(
            self.context, {100, 200}, frozenset(),
        ))

    async def test_private_source_requires_same_audience(self):
        overwrite = {self.lead: SimpleNamespace(
            view_channel=True, read_message_history=True,
        )}
        source = self.channel(200, allow={self.lead.id}, overwrites=overwrite)
        destination = self.channel(100, public=True)
        self.context.source_message.channel = destination
        self.assertFalse(await can_disclose_provenance(
            self.context, {200}, frozenset(),
        ))

        destination.public = False
        destination.allow = frozenset({self.lead.id})
        destination.overwrites = dict(overwrite)
        self.assertTrue(await can_disclose_provenance(
            self.context, {200}, frozenset(),
        ))

    async def test_matching_overwrites_do_not_override_effective_role_readership(self):
        overwrite = {self.lead: SimpleNamespace(
            view_channel=True, read_message_history=True,
        )}
        self.channel(200, allow={self.lead.id}, overwrites=dict(overwrite))
        destination = self.channel(100, public=True, overwrites=dict(overwrite))
        self.context.source_message.channel = destination

        self.assertFalse(await can_disclose_provenance(
            self.context, {200}, frozenset(),
        ))

    async def test_role_restricted_data_needs_role_restricted_destination(self):
        destination = self.channel(100, public=True)
        self.context.source_message.channel = destination
        self.assertFalse(await can_disclose_provenance(
            self.context, {100}, {ACCESS_LEAD},
        ))

        self.member = _Member(42, (self.core,))
        self.context.member = self.member
        self.assertFalse(await can_disclose_provenance(
            self.context, {100}, {ACCESS_LEAD},
        ))
        self.member = _Member(42, (self.lead, self.core))
        self.context.member = self.member

        destination.public = False
        destination.allow = frozenset({self.lead.id})
        destination.overwrites = {self.lead: SimpleNamespace(
            view_channel=True, read_message_history=True,
        )}
        self.assertTrue(await can_disclose_provenance(
            self.context, {100}, {ACCESS_LEAD},
        ))

        destination.overwrites[self.member] = SimpleNamespace(
            view_channel=True, read_message_history=True,
        )
        self.assertFalse(await can_disclose_provenance(
            self.context, {100}, {ACCESS_LEAD},
        ))

    async def test_restricted_tool_result_is_rolled_back_before_model_receives_it(self):
        destination = self.channel(100, public=True)
        self.channel(200, allow={self.lead.id}, overwrites={
            self.lead: SimpleNamespace(view_channel=True, read_message_history=True),
        })
        context = AgentRequestContext(
            bot=SimpleNamespace(), guild=self.guild, member=self.member,
            source_message=SimpleNamespace(
                id=1000, channel=destination,
                created_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
            ),
            account_links=None, clan_health=None, message_search=None,
        )
        context.state.source_channels.add(100)

        async def lookup(context, arguments):
            context.state.source_channels.add(200)
            return {"secret": "restricted detail"}

        tool = RegisteredAgentTool(
            AgentToolDefinition("lookup", "test", {"properties": {}}), lookup,
        )
        session = SimpleNamespace(advance=AsyncMock(side_effect=[
            AgentStep("", (plan_call(AgentToolCall("lookup", "lookup", "{}")),), AgentUsage()),
            AgentStep("The source cannot be used here.", (), AgentUsage()),
        ]))
        model = SimpleNamespace(create_agent_session=lambda **kwargs: session)
        with patch("elbow_helper.features.agent.service.build_agent_tools", return_value={"lookup": tool}):
            answer = await AgentService(model).answer(
                question="Check the record", local_context="", context=context,
            )

        self.assertEqual(answer, "The source cannot be used here.")
        result = json.loads(session.advance.await_args_list[1].args[0][0].content)
        self.assertEqual(result["results"]["lookup"]["flags"]["status"], "failed")
        self.assertNotIn("restricted detail", str(session.advance.await_args_list))
        self.assertEqual(context.state.source_channels, {100})
        self.assertFalse(any("restricted detail" in item for item in context.state.evidence))
