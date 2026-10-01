"""Hibernation action previews the saved state and member changes."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.commands.adapters.hibernation import (
    hibernation_adapters, prepare_hibernate,
)


class HibernationCommandTests(unittest.IsolatedAsyncioTestCase):
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
        change = await prepare_hibernate(context, {"user": 3})
        self.assertTrue(await change.preview.recheck())
        self.assertIn("Remove <@&20> from <@3>.", change.preview.lines)
        self.assertIn("Reactivate when ready.", change.preview.lines)
        workflow.hibernate_member.assert_not_awaited()
        result = await change.run()
        self.assertEqual(result.after["state"]["roles"], [20])
        self.assertIs(hibernation_adapters()[0].classification,
                      ActionClass.IRREVERSIBLE)


if __name__ == "__main__":
    unittest.main()
