"""Reactions and pins act on the selected message with fresh checks."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.models import AgentTurnState
from elbow_helper.agent.discord_actions.message_controls import (
    prepare_control_undo, prepare_pin, prepare_reaction,
)


class DiscordMessageControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        message = SimpleNamespace(
            id=6, jump_url="https://discord.example/message", reactions=[], pinned=False,
        )
        async def add(emoji):
            message.reactions.append(SimpleNamespace(emoji=emoji, me=True))
        async def remove(emoji, member):
            message.reactions = [item for item in message.reactions
                                 if not (str(item.emoji) == emoji and item.me)]
        async def pin(*, reason):
            message.pinned = True
        async def unpin(*, reason):
            message.pinned = False
        message.add_reaction = AsyncMock(side_effect=add)
        message.remove_reaction = AsyncMock(side_effect=remove)
        message.pin = AsyncMock(side_effect=pin)
        message.unpin = AsyncMock(side_effect=unpin)
        self.permissions = dict(view_channel=True, read_message_history=True,
                                add_reactions=True, manage_messages=True)
        channel = SimpleNamespace(
            id=2, mention="#place", guild=SimpleNamespace(id=3),
            fetch_message=AsyncMock(return_value=message),
            permissions_for=lambda _: SimpleNamespace(**self.permissions),
        )
        member = SimpleNamespace(id=4)
        guild = SimpleNamespace(
            id=3, me=SimpleNamespace(id=1), get_channel_or_thread=lambda _: channel,
            get_member=lambda _: member,
        )
        self.context = SimpleNamespace(
            guild=guild, bot=SimpleNamespace(), member=member,
            state=AgentTurnState(),
        )
        self.message = message
        self.patch = patch(
            "elbow_helper.agent.discord_actions.message_controls.require_evidence_access",
            new_callable=AsyncMock,
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_reaction_undo_changes_only_the_bot_reaction(self):
        self.message.reactions.append(SimpleNamespace(emoji="X", me=False))
        arguments = {"channel_id": 2, "message_id": 6,
                     "operation": "remove", "emoji": "X"}
        self.assertEqual((await prepare_reaction(self.context, arguments))["status"], "no_change")
        arguments["operation"] = "add"
        await prepare_reaction(self.context, arguments)
        action = self.context.state.proposed_changes.pop()
        self.assertEqual(action.preview.lines[0],
                         f"😀 Add X to {self.message.jump_url}")
        result = await action.run()
        self.assertTrue(await action.verify())
        undo = await prepare_control_undo(self.context, {
            "targets": action.values, "before": action.preview.before,
            "after": result.after,
        })
        self.assertEqual(undo.preview.lines[0],
                         f"😀 Remove X from {self.message.jump_url}")
        await undo.run()
        self.assertEqual(len(self.message.reactions), 1)
        self.assertFalse(self.message.reactions[0].me)

    async def test_pins_and_reactions_need_the_askers_own_permission(self):
        self.message.reactions.append(SimpleNamespace(emoji="X", me=True))
        pin = {"channel_id": 2, "message_id": 6, "operation": "pin"}
        add = {"channel_id": 2, "message_id": 6, "operation": "add", "emoji": "Y"}
        remove = {"channel_id": 2, "message_id": 6, "operation": "remove", "emoji": "X"}
        self.permissions.update(manage_messages=False, add_reactions=False)
        for prepare, arguments in ((prepare_pin, pin), (prepare_reaction, add),
                                   (prepare_reaction, remove)):
            self.assertEqual((await prepare(self.context, arguments))["cause"], "access_denied")
        self.permissions.update(add_reactions=True)
        self.assertEqual((await prepare_reaction(self.context, add))["status"],
                         "confirmation_required")
        self.assertEqual((await prepare_reaction(self.context, remove))["cause"], "access_denied")

    async def test_pin_and_unpin_recheck_the_message(self):
        await prepare_pin(self.context, {
            "channel_id": 2, "message_id": 6, "operation": "pin",
        })
        action = self.context.state.proposed_changes.pop()
        self.assertTrue(await action.preview.recheck())
        await action.run()
        self.assertTrue(self.message.pinned)
        await prepare_pin(self.context, {
            "channel_id": 2, "message_id": 6, "operation": "unpin",
        })
        await self.context.state.proposed_changes.pop().run()
        self.assertFalse(self.message.pinned)
