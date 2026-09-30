"""Thread actions recheck their targets and expose creation results."""

from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.tools.discord_threads import (
    prepare_create_thread, prepare_thread_members, prepare_update_thread,
)


class FakeThread:
    def __init__(self):
        self.id = 7
        self.name = "Before"
        self.archived = False
        self.locked = False
        self.mention = "#thread"
        self.members = set()
        self.guild = SimpleNamespace(id=3)

    def permissions_for(self, actor):
        return SimpleNamespace(view_channel=True)

    async def edit(self, *, reason, **values):
        for key, value in values.items():
            setattr(self, key, value)
        return self

    async def fetch_members(self):
        return [SimpleNamespace(id=value) for value in self.members]

    async def add_user(self, member):
        self.members.add(member.id)

    async def remove_user(self, member):
        self.members.remove(member.id)


class FakeTextChannel:
    def __init__(self, thread):
        self.id = 2
        self.mention = "#parent"
        self.guild = SimpleNamespace(id=3)
        self.create_thread = AsyncMock(return_value=thread)

    def permissions_for(self, actor):
        return SimpleNamespace(
            view_channel=True, create_public_threads=True,
            create_private_threads=True,
        )


class FakeForumChannel:
    pass


class DiscordThreadActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.thread = FakeThread()
        self.parent = FakeTextChannel(self.thread)
        self.member = SimpleNamespace(id=4, mention="@member",
                                      top_role=SimpleNamespace(position=1))
        bot_member = SimpleNamespace(id=1, top_role=SimpleNamespace(position=10))
        guild = SimpleNamespace(
            id=3, me=bot_member,
            get_channel_or_thread=lambda value: self.parent if value == 2 else self.thread,
            get_member=lambda _: self.member,
            fetch_member=AsyncMock(return_value=self.member),
        )
        self.context = SimpleNamespace(
            guild=guild, bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=self.thread)),
            member=SimpleNamespace(id=5, display_name="Asker"), state=AgentTurnState(),
        )
        self.patches = ExitStack()
        self.patches.enter_context(patch(
            "elbow_helper.features.agent.tools.discord_threads.discord.Thread", FakeThread,
        ))
        self.patches.enter_context(patch(
            "elbow_helper.features.agent.tools.discord_threads.discord.TextChannel", FakeTextChannel,
        ))
        self.patches.enter_context(patch(
            "elbow_helper.features.agent.tools.discord_threads.discord.ForumChannel", FakeForumChannel,
        ))
        self.patches.enter_context(patch(
            "elbow_helper.features.agent.tools.discord_threads.require_evidence_access",
            new_callable=AsyncMock,
        ))
        self.addCleanup(self.patches.close)

    async def test_create_thread_returns_a_later_action_target(self):
        result = await prepare_create_thread(self.context, {
            "parent_channel_id": 2, "name": "Before",
        })
        self.assertEqual(result["status"], "confirmation_required")
        action = self.context.state.command_proposals.pop()
        self.assertTrue(await action.preview.recheck())
        outcome = await action.run()
        self.assertEqual(outcome.result["thread_id"], self.thread.id)
        self.assertTrue(await action.verify())
        self.parent.create_thread.assert_awaited_once()

    async def test_update_thread_checks_the_old_value(self):
        result = await prepare_update_thread(self.context, {
            "thread_id": 7, "operation": "rename", "name": "After",
        })
        self.assertEqual(result["status"], "confirmation_required")
        action = self.context.state.command_proposals.pop()
        self.assertTrue(await action.preview.recheck())
        await action.run()
        self.assertEqual(self.thread.name, "After")
        self.assertTrue(await action.verify())

    async def test_member_changes_are_separate_steps(self):
        result = await prepare_thread_members(self.context, {
            "thread_id": 7, "operation": "add", "member_ids": [4],
        })
        self.assertEqual(result["prepared_count"], 1)
        action = self.context.state.command_proposals.pop()
        self.assertTrue(await action.preview.recheck())
        await action.run()
        self.assertIn(4, self.thread.members)

    async def test_new_thread_can_be_updated_and_joined_in_the_same_run(self):
        reference = {"step": "created", "path": ["thread_id"]}
        await prepare_create_thread(self.context, {
            "parent_channel_id": 2, "name": "Planned",
        })
        created = self.context.state.command_proposals.pop()
        self.context.state.command_proposals.append(replace(created, step_id="created"))
        update = await prepare_update_thread(self.context, {
            "thread_id": reference, "operation": "rename", "name": "After",
        })
        members = await prepare_thread_members(self.context, {
            "thread_id": reference, "operation": "add", "member_ids": [4],
        })
        self.assertEqual(update["status"], "confirmation_required")
        self.assertEqual(members["prepared_count"], 1)
        _, changing, joining = self.context.state.command_proposals
        self.assertIn("thread Planned", changing.preview.lines[0])
        self.assertNotIn("created", changing.preview.lines[0])
        bound_change = await changing.bind({"created": {"thread_id": 7}})
        bound_join = await joining.bind({"created": {"thread_id": 7}})
        self.assertTrue(await bound_change.preview.recheck())
        await bound_change.run()
        self.assertTrue(await bound_join.preview.recheck())
        await bound_join.run()
        self.assertEqual(self.thread.name, "After")
        self.assertIn(4, self.thread.members)
