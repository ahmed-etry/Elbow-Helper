"""Role-owned workflows with Discord's real overwrite resolution."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from features.agent.engine.helpers import LegacyCatalogueMixin

import unittest
from unittest.mock import patch

import discord

from elbow_helper.configuration import channels, roles
from elbow_helper.features.agent.access import (
    accessible_message_channel, assert_answer_scope, has_access_requirements,
)
from elbow_helper.features.agent.disclosure import destination_clearance, source_access_levels
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import AgentRequestContext, AgentTurnState
from elbow_helper.features.agent.research.history import searchable_channel_ids
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from features.agent.engine.test_agent_plan_flow import _Model, _Session, _model_step, _plan


class RoleCategoryScopeTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = discord.Client(intents=discord.Intents.all())
        self.client._connection.user = SimpleNamespace(id=99)
        self.recruiter = next(iter(roles.RECRUITERS))
        self.examiner = next(iter(roles.EXAMINERS))
        self.core = next(iter(roles.CORE))
        read = discord.Permissions(view_channel=True, read_message_history=True).value
        identifiers = {1, self.recruiter, self.examiner, self.core,
                       roles.MEMBER_ROLE_ID, roles.AGENT_TESTER_ROLE_ID, 77}
        self.guild = discord.Guild(state=self.client._connection, data={
            "id": "1", "name": "Synthetic guild", "owner_id": "10", "member_count": 4,
            "roles": [{"id": str(identifier), "name": str(identifier),
                       "permissions": str(read if identifier == 1 else 0),
                       **({"managed": True, "tags": {"bot_id": "99"}} if identifier == 77 else {})}
                      for identifier in identifiers],
            "members": [{"user": {"id": str(identifier), "username": str(identifier),
                                  "discriminator": "0", "avatar": None, "bot": identifier == 99},
                         "roles": [str(role) for role in member_roles],
                         "joined_at": "2026-01-01T00:00:00+00:00", "flags": 0}
                        for identifier, member_roles in (
                            (10, [self.recruiter, self.examiner, roles.AGENT_TESTER_ROLE_ID]),
                            (11, [self.core]), (12, [roles.MEMBER_ROLE_ID]), (99, [77]))],
        })
        self.room = self.channel(channels.REC_ROOM, channels.RECRUITMENT_CATEGORY,
                                 grants=[self.recruiter, self.core, 77])
        self.support = self.channel(200, channels.RECRUITMENT_CATEGORY, grants=[77], member_grant=10)
        self.ticket = self.channel(201, channels.RECRUITMENT_TICKET_CATEGORY,
                                  grants=[self.recruiter, 77], member_grant=12)
        self.exam_room = self.channel(channels.EXAMINATION_ROOM, channels.PROMOTION_CATEGORY,
                                      grants=[self.examiner, self.core, 77])
        self.promo = self.channel(202, channels.EXAMINATION_TICKET_CATEGORY,
                                 grants=[self.examiner, 77], member_grant=12)
        self.context = AgentRequestContext(
            bot=SimpleNamespace(user=self.guild.me), guild=self.guild,
            member=self.guild.get_member(10), source_message=SimpleNamespace(
                id=1000, channel=self.room, created_at=datetime.now(timezone.utc)),
            account_links=None, message_search=None,
        )

    def channel(self, identifier, category, *, grants=(), member_grant=None, deny=None):
        overwrites = [{"id": "1", "type": 0, "allow": "0",
                       "deny": str(discord.Permissions(view_channel=True).value)}]
        for role in grants:
            overwrites.append({"id": str(role), "type": 0,
                               "allow": str(discord.Permissions(view_channel=True).value), "deny": "0"})
        if member_grant:
            overwrites.append({"id": str(member_grant), "type": 1,
                               "allow": str(discord.Permissions(view_channel=True).value), "deny": "0"})
        if deny:
            overwrites.append({"id": str(deny), "type": 1, "allow": "0",
                               "deny": str(discord.Permissions(read_message_history=True).value)})
        channel = discord.TextChannel(state=self.client._connection, guild=self.guild, data={
            "id": str(identifier), "type": 0, "name": str(identifier),
            "parent_id": str(category), "position": 0, "permission_overwrites": overwrites,
        })
        self.guild._add_channel(channel)
        return channel

    async def test_category_grants_do_not_turn_workflow_sources_into_personal_sources(self):
        self.assertEqual(source_access_levels(self.support, self.guild), {"recruitment"})
        self.assertEqual(await accessible_message_channel(self.context, self.support.id), self.support)
        self.assertIn("recruitment", await destination_clearance(self.room, self.guild))
        self.assertIn("promotion", await destination_clearance(self.exam_room, self.guild))
        for category, level in ((channels.CWL_CATEGORY, "cwl"),
                                (channels.LEADERSHIP_CATEGORY, "lead_plus_or_core"),
                                (channels.PROMOTION_CATEGORY, "promotion")):
            source = self.channel(300 + category, category, grants=[77], member_grant=10)
            self.assertEqual(source_access_levels(source, self.guild), {level})

    async def test_incomplete_member_cache_proves_core_and_department_audiences_from_roles(self):
        self.guild._member_count += 1
        self.assertFalse(self.guild.chunked)
        self.assertIn("recruitment", await destination_clearance(self.room, self.guild))
        self.assertIn("promotion", await destination_clearance(self.exam_room, self.guild))

    async def test_core_can_read_workflow_tickets_without_a_personal_ticket_grant(self):
        self.context = replace(self.context, member=self.guild.get_member(11))
        for level in ("recruiter", "examiner", "cwl_helper", "lead_plus"):
            self.assertFalse(has_access_requirements(self.guild, 11, {level}))
        self.assertFalse(self.ticket.permissions_for(self.context.member).view_channel)
        self.assertEqual(await accessible_message_channel(self.context, self.ticket.id), self.ticket)
        self.context.source_message.channel = self.exam_room
        self.context.state.scope = None
        self.assertFalse(self.promo.permissions_for(self.context.member).view_channel)
        self.assertEqual(await accessible_message_channel(self.context, self.promo.id), self.promo)

    async def test_read_denials_keep_personal_boundaries_and_member_channels_stay_member_level(self):
        for category in (channels.RECRUITMENT_CATEGORY, channels.PROMOTION_CATEGORY,
                         channels.RECRUITMENT_TICKET_CATEGORY, channels.EXAMINATION_TICKET_CATEGORY):
            narrow = self.channel(400 + category, category, grants=[77], member_grant=10, deny=11)
            self.assertIsNone(source_access_levels(narrow, self.guild))
            self.assertIsNone(await accessible_message_channel(self.context, narrow.id))
        public = self.channel(500, channels.PROMOTION_CATEGORY, grants=[roles.MEMBER_ROLE_ID, 77])
        self.assertEqual(source_access_levels(public, self.guild), set())
        support = self.channel(501, channels.SUPPORT_TICKET_CATEGORY, grants=[77], member_grant=10)
        self.assertIsNone(source_access_levels(support, self.guild))
        self.assertIsNone(await accessible_message_channel(self.context, support.id))
        for identifier in (502, channels.HIBERNATION_LOG):
            restricted = self.channel(identifier, 900, grants=[self.recruiter], deny=11)
            self.assertIsNone(source_access_levels(restricted, self.guild))

    async def test_recruitment_and_promotion_tickets_reach_the_model_and_public_answer(self):
        for room, ticket, note in ((self.room, self.ticket, "Recruiter ticket discussion."),
                                   (self.exam_room, self.promo, "Examiner promotion notes.")):
            with self.subTest(room=room.id):
                self.context = replace(self.context, state=AgentTurnState())
                self.context.source_message.channel = room
                self.assertIn(ticket.id, await searchable_channel_ids(self.context))
                step = {"id": "ticket", "capability": "read_message_context",
                        "arguments": {"channel_id": ticket.id, "message_id": 123}, "depends_on": []}
                session = _Session([_model_step(_plan([step])), AgentStep(note, (), AgentUsage())], [])
                tool = build_agent_tools()["read_message_context"]

                async def history(channel, **kwargs):
                    self.assertEqual(channel.id, ticket.id)
                    yield SimpleNamespace(id=123, channel=channel, guild=self.guild,
                                          author=self.guild.get_member(10), content=note, embeds=[],
                                          attachments=[], reference=None, created_at=datetime.now(timezone.utc))

                with patch.object(discord.TextChannel, "history", history), patch(
                    "elbow_helper.features.agent.engine.service.build_agent_tools",
                    return_value={"read_message_context": tool},
                ):
                    answer = await AgentService(_Model(session)).answer(
                        question="What did the reviewers say in this ticket?",
                        local_context="", context=self.context,
                    )
                self.assertEqual(answer, note)
                self.assertIn(note, str(session.calls))
                self.assertIn(ticket.id, self.context.state.source_channels)
                self.assertFalse(self.context.state.scope_audit.excluded)
                self.assertFalse(self.context.state.scope_audit.offered)
                await assert_answer_scope(self.context, answer)
