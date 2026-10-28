from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from agent.engine.test_agent_service import _AgentModel
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.roles import (
    CORE, LEAD, AGENT_TESTER_ROLE_ID, SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID,
)
from elbow_helper.agent.access import ACCESS_LEAD, has_access_requirements, lookup_level
from elbow_helper.agent.disclosure import can_disclose_provenance
from elbow_helper.agent.models import AgentRequestContext, AgentTurnState, RegisteredAgentTool
from elbow_helper.agent.engine.service import AgentService
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
        managed = SimpleNamespace(id=77, managed=True, tags=SimpleNamespace(bot_id=99))
        self.guild.roles.append(managed)
        self.assertTrue(await can_disclose_provenance(self.context, {100}, {ACCESS_LEAD}))

    def setUp(self):
        self.default = _Role(1)
        self.lead = _Role(next(iter(LEAD)))
        self.core = _Role(next(iter(CORE - LEAD)))
        self.member = _Member(42, (self.lead, self.core))
        self.bot_member = _Member(99, ())
        self.guild = SimpleNamespace(
            id=1, name="Brown Elbow", default_role=self.default, owner_id=42,
            roles=[self.default, self.lead, self.core],
            me=self.bot_member,
            get_member=lambda member_id: self.member if member_id == 42 else None,
        )
        self.channels = {}
        self.guild.get_channel_or_thread = self.channels.get
        self.context = SimpleNamespace(
            guild=self.guild, member=self.member, state=AgentTurnState(),
            bot=SimpleNamespace(), source_message=SimpleNamespace(channel=None),
        )

    def channel(self, channel_id, **kwargs):
        channel = _Channel(channel_id, self.guild, **kwargs)
        self.channels[channel_id] = channel
        return channel

    async def test_lead_evidence_can_be_answered_to_sleeping_leadership_audiences(self):
        for role_id in (SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID):
            for complete_cache in (False, True):
                with self.subTest(role_id=role_id, complete_cache=complete_cache):
                    sleeping = _Role(role_id)
                    reader = _Member(43, (sleeping,))
                    self.guild.roles = [self.default, self.lead, self.core, sleeping]
                    self.guild.chunked = complete_cache
                    self.guild.members = [self.member, reader, self.bot_member]
                    self.guild.get_member = lambda identifier: next(
                        (member for member in self.guild.members if member.id == identifier), None)
                    destination = self.channel(100, allow={self.lead.id, role_id}, overwrites={
                        self.lead: SimpleNamespace(view_channel=True, read_message_history=True),
                        sleeping: SimpleNamespace(view_channel=True, read_message_history=True)})
                    self.context.source_message.channel = destination
                    self.context.state.scope = None
                    self.assertTrue(await can_disclose_provenance(self.context, {100}, {'lead', 'lead_plus'}))
                    self.assertFalse(has_access_requirements(self.guild, reader.id, {'lead'}))
                    self.assertFalse(has_access_requirements(self.guild, reader.id, {'lead_plus'}))

    async def test_agent_delivers_lead_finding_in_a_room_sleeping_co_can_view(self):
        sleeping = _Role(SLEEPING_CO_ROLE_ID)
        self.guild.roles.append(sleeping)
        destination = self.channel(100, allow={self.lead.id, sleeping.id}, overwrites={
            role: SimpleNamespace(view_channel=True, read_message_history=True)
            for role in (self.lead, sleeping)})
        context = AgentRequestContext(bot=SimpleNamespace(), guild=self.guild, member=self.member,
            source_message=SimpleNamespace(id=1000, channel=destination,
                created_at=datetime(2026, 9, 24, tzinfo=timezone.utc)),
            account_links=None, message_search=None)

        @lookup_level(ACCESS_LEAD)
        async def lookup(context, arguments):
            context.state.source_channels.add(destination.id)
            return {'finding': 'Seven confirmed participants.'}

        tool = RegisteredAgentTool(AgentToolDefinition('lookup', 'Read a finding.', {'properties': {}}), lookup)
        session = SimpleNamespace(advance=AsyncMock(side_effect=[
            AgentStep('', (plan_call(AgentToolCall('lookup', 'lookup', '{}')),), AgentUsage()),
            AgentStep('Seven confirmed participants.', (), AgentUsage())]))
        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value={'lookup': tool}):
            answer = await AgentService(_AgentModel(session)).answer(
                question='How many participants are confirmed?', local_context='', context=context)
        self.assertEqual(answer, 'Seven confirmed participants.')
        self.assertEqual(context.state.required_access, {ACCESS_LEAD})

    async def test_sleeping_leader_can_read_without_receiving_action_permissions(self):
        sleeping, tester = _Role(SLEEPING_CO_ROLE_ID), _Role(AGENT_TESTER_ROLE_ID)
        self.guild.roles.extend([sleeping, tester])
        self.member = _Member(42, (sleeping, tester))
        destination = self.channel(100, allow={sleeping.id}, overwrites={
            sleeping: SimpleNamespace(view_channel=True, read_message_history=True)})
        context = AgentRequestContext(bot=SimpleNamespace(), guild=self.guild, member=self.member,
            source_message=SimpleNamespace(id=1000, channel=destination,
                created_at=datetime(2026, 9, 24, tzinfo=timezone.utc)),
            account_links=None, message_search=None)

        @lookup_level(ACCESS_LEAD)
        async def lookup(context, arguments):
            return {'finding': 'The next event is Saturday.'}

        result = await lookup(context, {})
        self.assertEqual(result['finding'], 'The next event is Saturday.')
        self.assertEqual(context.state.required_access, {ACCESS_LEAD})
        from elbow_helper.agent.access import require_evidence_access
        await require_evidence_access(context)
        self.assertFalse(has_access_requirements(self.guild, self.member.id, {ACCESS_LEAD}))

    async def test_same_channel_and_public_source_can_be_disclosed(self):
        destination = self.channel(100, public=True)
        self.channel(200, public=True)
        self.context.source_message.channel = destination

        self.assertTrue(await can_disclose_provenance(
            self.context, {100, 200}, frozenset(),
        ))

    async def test_private_source_requires_same_audience(self):
        overwrite = {self.lead: SimpleNamespace(
            view_channel=True, read_message_history=True,
        )}
        self.channel(200, allow={self.lead.id}, overwrites=overwrite)
        destination = self.channel(100, public=True)
        self.context.source_message.channel = destination
        self.assertFalse(await can_disclose_provenance(
            self.context, {200}, frozenset(),
        ))

        self.context.state.scope = None
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

        self.context.state.scope = None
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
        self.assertTrue(await can_disclose_provenance(
            self.context, {100}, {ACCESS_LEAD},
        ))
        destination.overwrites[_Member(43, ())] = SimpleNamespace(
            view_channel=True, read_message_history=True,
        )
        self.assertFalse(await can_disclose_provenance(
            self.context, {100}, {ACCESS_LEAD},
        ))

    async def test_incomplete_cache_requires_authorized_owner_despite_role_overwrites(self):
        destination = self.channel(100, allow={self.lead.id})
        self.context.source_message.channel = destination
        for owner_id in (None, 43):
            with self.subTest(owner_id=owner_id):
                self.guild.owner_id = owner_id
                self.assertFalse(await can_disclose_provenance(self.context, {100}, {ACCESS_LEAD}))
        owner = _Member(43, ())
        self.guild.get_member = lambda identifier: self.member if identifier == 42 else owner
        self.assertFalse(await can_disclose_provenance(self.context, {100}, {ACCESS_LEAD}))
        self.guild.owner_id = 42
        self.assertTrue(await can_disclose_provenance(self.context, {100}, {ACCESS_LEAD}))

    async def test_requester_can_read_evidence_for_a_private_answer(self):
        destination = self.channel(100, public=True)
        self.channel(200, allow={self.lead.id}, overwrites={
            self.lead: SimpleNamespace(view_channel=True, read_message_history=True),
        })
        context = AgentRequestContext(private_pass=True,
            bot=SimpleNamespace(), guild=self.guild, member=self.member,
            source_message=SimpleNamespace(
                id=1000, channel=destination,
                created_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
            ),
            account_links=None, message_search=None,
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
        model = _AgentModel(session)
        with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value={"lookup": tool}):
            answer = await AgentService(model).answer(
                question="Check the record", local_context="", context=context,
            )

        self.assertEqual(answer, "The source cannot be used here.")
        result = json.loads(session.advance.await_args_list[1].args[0][0].content)
        self.assertEqual(result["results"]["lookup"]["flags"]["status"], "complete")
        self.assertIn("restricted detail", str(session.advance.await_args_list))
        self.assertEqual(context.state.source_channels, {100, 200})
        self.assertTrue(any("restricted detail" in item for item in context.state.evidence))
