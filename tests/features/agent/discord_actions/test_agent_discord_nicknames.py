"""Nickname actions preserve the prior name for confirmed undo."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.discord_actions.nicknames import (
    prepare_nickname, prepare_nickname_undo,
)


class DiscordNicknameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        member = SimpleNamespace(
            id=4, mention="@member", nick="Before",
            top_role=SimpleNamespace(position=1),
        )
        async def edit(*, nick, reason):
            member.nick = nick
        member.edit = AsyncMock(side_effect=edit)
        guild = SimpleNamespace(
            id=2, me=SimpleNamespace(id=1, top_role=SimpleNamespace(position=10)),
            get_member=lambda _: member, fetch_member=AsyncMock(return_value=member),
        )
        self.context = SimpleNamespace(
            guild=guild, member=SimpleNamespace(id=5, display_name="Asker"),
            state=AgentTurnState(),
        )
        self.context.member.top_role = SimpleNamespace(position=5)
        self.context.member.guild_permissions = SimpleNamespace(manage_nicknames=True)
        original_get_member = guild.get_member
        guild.get_member = lambda identifier: self.context.member if identifier == self.context.member.id else original_get_member(identifier)
        self.member = member
        self.patch = patch(
            "elbow_helper.features.agent.discord_actions.nicknames.require_evidence_access",
            new_callable=AsyncMock,
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_set_and_undo_recheck_the_prior_nickname(self):
        result = await prepare_nickname(self.context, {
            "member_id": 4, "nickname": "After",
        })
        self.assertEqual(result["status"], "confirmation_required")
        action = self.context.state.command_proposals.pop()
        self.assertTrue(await action.preview.recheck())
        changed = await action.run()
        self.assertEqual(self.member.nick, "After")
        self.assertTrue(await action.verify())
        undo = await prepare_nickname_undo(self.context, {
            "targets": action.values, "before": action.preview.before,
            "after": changed.after,
        })
        self.assertTrue(await undo.preview.recheck())
        await undo.run()
        self.assertEqual(self.member.nick, "Before")

    async def test_omitting_nickname_resets_it(self):
        await prepare_nickname(self.context, {"member_id": 4})
        action = self.context.state.command_proposals.pop()
        self.assertIn("Reset", action.preview.lines[0])
        await action.run()
        self.assertIsNone(self.member.nick)
