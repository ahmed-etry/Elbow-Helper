"""Hibernation action previews the saved state and member changes."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.capabilities.hibernation.commands import (
    hibernation_adapters, prepare_hibernate, prepare_reactivate,
)
from features.agent.discord_actions.helpers import register_requester


class HibernationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_reactivate_previews_restored_roles_and_ticket(self):
        role = SimpleNamespace(
            id=20, mention="<@&20>", position=1, managed=False,
            permissions=SimpleNamespace(), is_default=lambda: False,
        )
        bot_member = SimpleNamespace(
            id=1, top_role=SimpleNamespace(position=10),
        )
        member = SimpleNamespace(
            id=3, mention="<@3>", roles=[],
            top_role=SimpleNamespace(position=2),
        )
        guild = SimpleNamespace(
            id=5, me=bot_member,
            get_member=lambda member_id: member if member_id == 3 else None,
            fetch_member=AsyncMock(return_value=member),
        )
        ticket = SimpleNamespace(id=99)
        plan = {
            "issue": None, "info": {"roles": [20]},
            "to_remove": (), "to_add": (role,),
            "missing_role_ids": (), "fallback_thread_id": 10,
            "ticket": {
                "name": "ticket-member", "category": None,
                "visible_role_ids": (), "bot_member_id": 1,
                "welcome": "Welcome back, <@3>!",
                "embed": discord.Embed(title="Reactivation Notice"),
            },
        }
        saved = {"roles": [20]}

        async def reactivate(_):
            nonlocal saved
            saved = None
            return {"message": "Welcome back. Your roles have been restored.",
                    "ticket": ticket, "ticket_channel_id": 99,
                    "member_id": 3}

        workflow = SimpleNamespace(
            prepare_reactivation=lambda **_: plan,
            reactivate_member=AsyncMock(side_effect=reactivate),
            hibernation_member_state=lambda _: saved,
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=guild, member=member,
        )
        context = register_requester(context)
        change = await prepare_reactivate(context, {})
        self.assertTrue(await change.preview.recheck())
        self.assertIn("Add <@&20> to <@3>.", change.preview.lines)
        self.assertIn("Create ticket **ticket-member**.", change.preview.lines)
        self.assertIn("Archive the hibernation thread <#10>.",
                      change.preview.lines)
        workflow.reactivate_member.assert_not_awaited()
        result = await change.run()
        self.assertEqual(result.after["ticket_channel_id"], 99)
        self.assertIs(next(adapter for adapter in hibernation_adapters()
                           if adapter.path == "/reactivate").classification,
                      ActionClass.CHANGE)

    async def test_hibernate_previews_roles_notice_and_persists_after_confirm(self):
        role = SimpleNamespace(
            id=20, mention="<@&20>", position=1, managed=False,
            permissions=SimpleNamespace(), is_default=lambda: False,
        )
        bot_member = SimpleNamespace(
            id=1, top_role=SimpleNamespace(position=10),
        )
        member = SimpleNamespace(
            id=3, mention="<@3>", roles=[role],
            top_role=SimpleNamespace(position=1),
        )
        guild = SimpleNamespace(
            id=5, me=bot_member,
            get_member=lambda member_id: member if member_id == 3 else None,
            fetch_member=AsyncMock(return_value=member),
        )
        plan = {
            "issue": None, "user": member, "stored_role_ids": [20],
            "snapshot_role_ids": [], "unix_ts": 100,
            "to_remove": (role,), "to_add": (),
            "missing_role_ids": (), "log_channel_id": 9,
            "fallback_channel_id": 10,
        }
        state = None

        async def hibernate(*_):
            nonlocal state
            state = {"roles": [20]}
            return "Moved <@3> to hibernation."

        workflow = SimpleNamespace(
            prepare_hibernation=lambda *_: plan,
            hibernation_notice_preview=lambda _: "Reactivate when ready.",
            hibernate_member=AsyncMock(side_effect=hibernate),
            hibernation_member_state=lambda _: state,
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=guild, member=SimpleNamespace(id=2),
        )
        context = register_requester(context)
        change = await prepare_hibernate(context, {"user": 3})
        self.assertTrue(await change.preview.recheck())
        self.assertIn("Remove <@&20> from <@3>.", change.preview.lines)
        self.assertIn("Reactivate when ready.", change.preview.details)
        workflow.hibernate_member.assert_not_awaited()
        result = await change.run()
        self.assertEqual(result.after["state"]["roles"], [20])
        self.assertIs(hibernation_adapters()[0].classification,
                      ActionClass.CHANGE)


if __name__ == "__main__":
    unittest.main()
