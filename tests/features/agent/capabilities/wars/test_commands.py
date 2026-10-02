from __future__ import annotations
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.capabilities.wars.statements import (
    war_statement_adapters,
)
from elbow_helper.features.wars.commands import WarStatements
from elbow_helper.features.wars.statements import CLAN_CHANNEL_MAP, WAR_STATEMENTS


class WarStatementCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_statements_use_their_feature_preview_and_post(self):
        clan, mapping = next(iter(CLAN_CHANNEL_MAP.items()))
        players = {member_id: SimpleNamespace(
            id=member_id, mention=f"<@{member_id}>",
        ) for member_id in (7, 8)}
        permissions = lambda _: SimpleNamespace(view_channel=True, send_messages=True)
        post_channel = SimpleNamespace(
            id=mapping["post_channel"], mention=f"<#{mapping['post_channel']}>",
            permissions_for=permissions, send=AsyncMock(),
        )
        war_channel = SimpleNamespace(
            id=mapping["clan_war_channel"], mention=f"<#{mapping['clan_war_channel']}>",
            permissions_for=permissions,
        )
        channels = {post_channel.id: post_channel, war_channel.id: war_channel}
        guild = SimpleNamespace(
            me=SimpleNamespace(id=3), get_member=lambda member_id: players.get(member_id),
            get_channel=lambda channel_id: channels.get(channel_id),
        )
        workflow = object.__new__(WarStatements)
        workflow.war_statements = WAR_STATEMENTS
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow), guild=guild,
            member=SimpleNamespace(id=4),
        )
        for adapter in war_statement_adapters():
            with self.subTest(command=adapter.path):
                values = {"clan": clan, "notes": "Check this."}
                if adapter.path.endswith("first-claim"):
                    values.update({"victim": 7, "attacker": 8})
                else:
                    values["players"] = "<@7> <@8>"
                preview = await adapter.prepare(context, values)
                self.assertTrue(await preview.recheck())
                self.assertIs(adapter.classification, ActionClass.CHANGE)
                self.assertIn("(blank line)", preview.details)
                self.assertNotIn("(blank line)", preview.lines)
                prior_count = post_channel.send.await_count
                outcome = await adapter.run(context, values)
                self.assertEqual(outcome.visibility, "private")
                self.assertEqual(post_channel.send.await_count, prior_count + 1)
                message = post_channel.send.await_args.args[0]
                self.assertIn("**Additional Notes:** Check this.", message)
                self.assertIn(post_channel.mention,
                              preview.lines[0])
