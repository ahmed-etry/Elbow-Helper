"""Verified promotion cases retain their audience outside ticket categories."""

from dataclasses import replace
import unittest
from unittest.mock import MagicMock

import discord

from elbow_helper.configuration import channels, roles
from elbow_helper.features.agent.access import accessible_message_channel
from elbow_helper.features.agent.capabilities.examination.reads import read_accessible_examination_cases
from elbow_helper.features.agent.disclosure import can_show_in_dm
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.examination.queries import ExaminationQueries
from features.agent import test_evidence_scope as fixtures

Channel, Role = fixtures.Channel, fixtures.Role


class ApprovedPromotionAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = fixtures.ScopeTests()
        fixture.setUp()
        self.context = replace(fixture.context, private_pass=True)
        self.context.source_message.channel = fixture.rec_room
        self.context.member.roles.append(Role(next(iter(roles.EXAMINERS))))
        self.cases = {"201": {"ticket_channel_id": 201, "type": "elder_promo"}}
        self.context = replace(self.context, examination_queries=ExaminationQueries(lambda: self.cases))
        self.ticket = fixture.personal
        self.ticket.viewers = {99}
        self.room = Channel(channels.EXAMINATION_ROOM, fixture.guild, {10, 99}, level=roles.EXAMINERS)
        fixture.guild.channels.append(self.room)

    async def test_all_approved_roles_can_read_registered_support_cases(self):
        for role in roles.EXAMINERS | roles.LEAD_PLUS:
            with self.subTest(role=role):
                self.context.member.roles = [Role(roles.AGENT_TESTER_ROLE_ID), Role(role)]
                self.context = replace(self.context, state=AgentTurnState())
                self.assertIs(await accessible_message_channel(self.context, 201), self.ticket)
                result = await read_accessible_examination_cases(self.context, {"ticket_channel_id": 201})
                self.assertEqual(result["cases"][0]["ticket_channel_id"], 201)
                self.assertIn(201, self.context.state.source_channels)

    async def test_unregistered_spoofed_and_invalid_cases_keep_personal_access(self):
        for state in ({}, {"201": {"ticket_channel_id": 999, "type": "elder_promo"}},
                      {"201": {"ticket_channel_id": 201, "type": "support"}}):
            with self.subTest(state=state):
                self.cases.clear()
                self.cases.update(state)
                self.context = replace(self.context, state=AgentTurnState())
                self.assertIsNone(await accessible_message_channel(self.context, 201))

    async def test_public_destination_and_explicit_denials_still_block(self):
        self.context = replace(self.context, private_pass=False)
        self.context.source_message.channel = self.context.guild.channels[1]
        self.assertIsNone(await accessible_message_channel(self.context, 201))
        self.context = replace(self.context, private_pass=True, state=AgentTurnState())
        self.ticket.overwrites[Role(10)] = discord.PermissionOverwrite(read_message_history=False)
        self.assertIsNone(await accessible_message_channel(self.context, 201))

    async def test_registered_private_thread_uses_workflow_identity_in_reads_and_dm(self):
        thread = MagicMock(spec=discord.Thread)
        thread.id, thread.guild, thread.parent = 201, self.context.guild, self.ticket
        thread.type = discord.ChannelType.private_thread
        thread.is_private.return_value = True
        thread.permissions_for = self.ticket.permissions_for
        self.context.guild.get_channel_or_thread = lambda identifier: thread if identifier == 201 else self.room
        self.assertIs(await accessible_message_channel(self.context, 201), thread)
        self.assertTrue(await can_show_in_dm(
            self.context.guild, 10, [thread], {"promotion"}, examination_queries=self.context.examination_queries,
        ))
        self.cases.clear()
        thread.fetch_member.side_effect = discord.NotFound(MagicMock(status=404), "missing")
        self.assertFalse(await can_show_in_dm(
            self.context.guild, 10, [thread], {"promotion"}, examination_queries=self.context.examination_queries,
        ))

    async def test_case_removal_revokes_retained_evidence(self):
        result = await read_accessible_examination_cases(self.context, {"ticket_channel_id": 201})
        self.assertIn("report_id", result)
        self.cases.clear()
        from elbow_helper.features.agent.access import AgentAccessLost, require_evidence_access
        with self.assertRaises(AgentAccessLost):
            await require_evidence_access(replace(self.context, state=self.context.state))
