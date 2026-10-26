"""Synthetic audit evidence reaches the model and remains private after permission checks."""

from dataclasses import replace
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.features.agent.access import ACCESS_VIEW_AUDIT_LOG, AgentAccessLost, LookupAccessDenied
from elbow_helper.features.agent.discord_actions.audit_log import read_audit
from elbow_helper.features.agent.models import EvidenceScope
from elbow_helper.features.agent.disclosure import can_show
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from features.agent.discord_actions.management_helpers import Entity, fixture
from features.agent.discord_actions.test_channel_management import history
from features.agent.files.test_agent_spreadsheets import _context


class AuditLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_entries_reach_the_model_with_filters_and_retained_permission(self):
        context, _, _, member = fixture()
        entries = [Entity(id=index, action=discord.AuditLogAction.role_update,
                          user=member, target=Entity(id=target, name="Synthetic role"),
                          created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                          reason="Synthetic reason", before=[("name", "Synthetic A")],
                          after=[("name", "Synthetic B")])
                   for index, target in ((1, 30), (2, 31))]
        context.private_pass = True
        context.state.scope = EvidenceScope(200, frozenset(), frozenset({ACCESS_VIEW_AUDIT_LOG}), True)
        context.guild.audit_logs = lambda **kwargs: history(entries)
        with patch("elbow_helper.features.agent.discord_actions.audit_log.require_evidence_access",
                   new_callable=AsyncMock):
            result = await read_audit(context, {"action_type": "role_update", "target_id": 30})
        self.assertEqual([row["entry_id"] for row in result["entries"]], [1])
        self.assertEqual(result["entries"][0]["changes"], [{
            "field": "name", "before": "Synthetic A", "after": "Synthetic B",
        }])
        self.assertIn(ACCESS_VIEW_AUDIT_LOG, context.state.required_access)
        self.assertFalse(context.state.proposed_changes)

    async def test_guild_audiences_are_refused_even_when_everyone_can_view_audit(self):
        context, channel, _, _ = fixture()
        self.assertFalse(await can_show(channel, {}, {ACCESS_VIEW_AUDIT_LOG}, context.guild))

    async def test_public_scope_refuses_audit_before_read(self):
        context, _, _, _ = fixture()
        context.guild.audit_logs = AsyncMock()
        from elbow_helper.features.agent.disclosure import request_scope
        await request_scope(context)
        with patch("elbow_helper.features.agent.discord_actions.audit_log.require_evidence_access", new_callable=AsyncMock):
            with self.assertRaises(LookupAccessDenied):
                await read_audit(context, {})
        context.guild.audit_logs.assert_not_called()

    async def test_missing_bot_permission_is_named_before_reading(self):
        context, _, _, _ = fixture()
        context.guild.me.guild_permissions = discord.Permissions.none()
        context.guild.audit_logs = AsyncMock()
        with patch("elbow_helper.features.agent.discord_actions.audit_log.require_evidence_access",
                   new_callable=AsyncMock):
            with self.assertRaisesRegex(ValueError, "The bot needs View Audit Log"):
                await read_audit(context, {})
        context.guild.audit_logs.assert_not_called()

    async def test_guard_aborts_contaminated_public_answer(self):
        with TemporaryDirectory() as directory:
            context = replace(_context(directory), private_pass=False)
            context.member.guild_permissions = discord.Permissions(view_audit_log=True)
            context.guild.me.guild_permissions = discord.Permissions(view_audit_log=True)
            context.state.required_access.add(ACCESS_VIEW_AUDIT_LOG)
            context.bot.user = Entity(id=999)
            message = Entity(id=501, author=context.member, guild=context.guild,
                             channel=context.source_message.channel, mentions=(),
                             reply=AsyncMock(), archive_reply=False)
            context = replace(context, source_message=message)
            surface = AgentDeliveryMixin()
            surface.bot = context.bot
            with self.assertRaises(AgentAccessLost):
                await surface.send_response(message, "Synthetic audit answer", None, context=context)
            message.reply.assert_not_awaited()
