"""Confirmed fake changes run once in the background and report in-channel."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.contracts import (
    ActionClass, ChangePreview, PreparedAction, check_bundle,
)
from elbow_helper.features.agent.actions.store import AgentActionRepository
from elbow_helper.features.agent.actions.runner import AgentActionRunner, StopActionRunView
from elbow_helper.features.agent.actions.outcomes import CommandOutcome
from elbow_helper.features.agent.actions.preview import preview_text
from elbow_helper.features.agent.actions.private_view import PrivateCommandView


class ActionRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = AgentActionRepository(Path(self.directory.name) / "actions.sqlite3")
        self.progress = SimpleNamespace(edit=AsyncMock())
        self.channel = SimpleNamespace(id=2, send=AsyncMock(return_value=self.progress))
        self.guild = SimpleNamespace(id=1, get_channel_or_thread=lambda _: self.channel)
        self.bot = SimpleNamespace(get_guild=lambda _: self.guild)
        self.context = SimpleNamespace(
            guild=self.guild, member=SimpleNamespace(id=4),
            source_message=SimpleNamespace(id=3, channel=self.channel),
        )
        self.runner = AgentActionRunner(
            bot=self.bot, repository=self.repository, guild_id=1,
        )
        await self.runner.recover()

    async def run_actions(self, *actions):
        with (patch("elbow_helper.features.agent.actions.runner.require_access"),
              patch("elbow_helper.features.agent.actions.runner.require_disclosure_access",
                    new_callable=AsyncMock)):
            run_id = await self.runner.submit(
                self.context, tuple(actions), confirmer_id=self.context.member.id,
            )
            await asyncio.gather(*tuple(self.runner._tasks))
        return self.repository.run(run_id)

    def action(self, name, *, outcome=None, allowed=True,
               action_class=ActionClass.CHANGE, verify=None):
        check = AsyncMock(return_value=allowed)
        run = AsyncMock(return_value=outcome or CommandOutcome("complete", text=name))
        return PreparedAction(
            name, {"target": name},
            ChangePreview((f"Change {name}",), check, before={"value": "old"}),
            run, action_class=action_class, verify=verify,
        ), check, run

    async def test_all_changes_run_once_and_report_without_button_followups(self):
        first, first_check, first_run = self.action("first")
        second, second_check, second_run = self.action("second")
        run = await self.run_actions(first, second)
        self.assertEqual(run["status"], "completed")
        self.assertEqual([step["status"] for step in run["steps"]],
                         ["completed", "completed"])
        first_check.assert_awaited_once()
        second_check.assert_awaited_once()
        first_run.assert_awaited_once()
        second_run.assert_awaited_once()
        self.assertEqual("Done: first (1), second (1).",
                         self.progress.edit.await_args.kwargs["content"])
        self.assertEqual(len(self.repository.recent_log(requester_id=4)), 2)

    async def test_wait_run_returns_the_finished_action_record(self):
        action, _, _ = self.action("first")
        with (patch("elbow_helper.features.agent.actions.runner.require_access"),
              patch("elbow_helper.features.agent.actions.runner.require_disclosure_access",
                    new_callable=AsyncMock)):
            run_id = await self.runner.submit(self.context, (action,), confirmer_id=4)
            finished = await self.runner.wait_run(run_id)
        self.assertEqual(finished["status"], "completed")

    async def test_failure_stops_before_later_change(self):
        first, _, first_run = self.action("first", allowed=False)
        second, _, second_run = self.action("second")
        run = await self.run_actions(first, second)
        first_run.assert_not_awaited()
        second_run.assert_not_awaited()
        self.assertEqual(run["status"], "failed")
        self.assertEqual([step["status"] for step in run["steps"]],
                         ["failed", "queued"])
        self.assertEqual("Done: none.\nNot done:\nChange first\nChange second",
                         self.progress.edit.await_args.kwargs["content"])

    async def test_adjacent_actions_share_one_report_group(self):
        actions = []
        for name in ("one", "two", "three"):
            action, _, _ = self.action(name, outcome=CommandOutcome("complete"))
            actions.append(replace(action, preview=replace(
                action.preview, summary="Add role",
            )))
        await self.run_actions(*actions)
        self.assertEqual("Done: Add role (3).",
                         self.progress.edit.await_args.kwargs["content"])
        self.channel.send.assert_awaited_once()

    async def test_long_remaining_lines_split_after_replacing_progress(self):
        action, _, _ = self.action("long", allowed=False)
        action = replace(action, preview=replace(
            action.preview, lines=("Not done: " + "x" * 2500,),
        ))
        await self.run_actions(action)
        self.assertLessEqual(len(self.progress.edit.await_args.kwargs["content"]), 2000)
        self.assertGreater(self.channel.send.await_count, 1)
        self.assertTrue(all(len(call.args[0]) <= 2000
                            for call in self.channel.send.await_args_list[1:]))

    async def test_stop_ends_after_current_change(self):
        first, _, first_run = self.action("first")
        second, _, second_run = self.action("second")
        async def stop_during_first():
            run_id = self.repository.recent_log(requester_id=4)[0]["run_id"]
            self.repository.request_stop(run_id, requester_id=4)
            return CommandOutcome("complete", text="first")
        first_run.side_effect = stop_during_first
        run = await self.run_actions(first, second)
        self.assertEqual(run["status"], "stopped")
        self.assertEqual([step["status"] for step in run["steps"]],
                         ["completed", "queued"])
        second_run.assert_not_awaited()

    async def test_only_confirmer_can_stop_a_run(self):
        view = StopActionRunView(self.repository, "synthetic", self.context.member.id)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=5), response=SimpleNamespace(
                send_message=AsyncMock(), defer=AsyncMock(),
            ),
        )
        await view.stop_run(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "Only the member who confirmed can stop this.", ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()

    async def test_private_result_is_behind_normal_message_button(self):
        action, _, _ = self.action("private", outcome=CommandOutcome(
            "complete", "private", private_parts=("secret",),
        ))
        await self.run_actions(action)
        view = self.progress.edit.await_args.kwargs["view"]
        self.assertIsInstance(view, PrivateCommandView)
        self.assertNotIn("secret", str(self.channel.send.await_args_list))

    async def test_uncertain_result_is_checked_without_running_twice(self):
        verify = AsyncMock(return_value=True)
        action, _, run = self.action("first", verify=verify)
        run.side_effect = OSError("uncertain")
        result = await self.run_actions(action)
        self.assertEqual(result["status"], "completed")
        run.assert_awaited_once()
        verify.assert_awaited_once()

    async def test_later_action_binds_an_earlier_action_result_once(self):
        first, _, first_run = self.action("first", outcome=CommandOutcome(
            "complete", result={"target_id": 7},
        ))
        first = replace(first, step_id="created")
        second_run = AsyncMock(return_value=CommandOutcome("complete"))
        placeholder = {"step": "created", "path": ["target_id"]}
        async def bind(results):
            target_id = results["created"]["target_id"]
            return PreparedAction(
                "second", {"target_id": target_id},
                ChangePreview(("Use the created target",), AsyncMock(return_value=True)),
                second_run,
            )
        second = PreparedAction(
            "second", {"target_id": placeholder},
            ChangePreview(("Use the target created by the first action",),
                          AsyncMock(return_value=True)),
            AsyncMock(), step_id="used", bind=bind,
        )
        result = await self.run_actions(first, second)
        self.assertEqual(result["status"], "completed")
        first_run.assert_awaited_once()
        second_run.assert_awaited_once()
        self.assertEqual(json.loads(result["steps"][1]["values_json"]), {"target_id": 7})
        log = self.repository.recent_log(requester_id=4)
        self.assertEqual(json.loads(log[0]["targets_json"]), {"target_id": 7})

    async def test_undo_restores_recorded_prior_state_after_a_new_preview(self):
        state = {"value": "old"}
        action, _, run = self.action("change")
        async def apply():
            state["value"] = "new"
            return CommandOutcome("complete", after={"value": "new"})
        run.side_effect = apply
        await self.run_actions(action)
        entry = self.repository.recent_log(requester_id=4)[0]

        async def undo_handler(context, log):
            async def execute_undo():
                state["value"] = log["before"]["value"]
                return CommandOutcome("complete", after=dict(state))
            return PreparedAction(
                "undo_change", log["targets"],
                ChangePreview(("Restore prior value",),
                              AsyncMock(return_value=state["value"] == log["after"]["value"]),
                              before=log["after"]),
                AsyncMock(side_effect=execute_undo),
            )

        self.runner.undo_handlers["change"] = undo_handler
        undo = await self.runner.prepare_undo(self.context, entry["log_id"])
        self.assertIn("Restore prior value", preview_text([undo]))
        await self.run_actions(undo)
        self.assertEqual(state["value"], "old")

    def test_irreversible_change_cannot_share_confirmation(self):
        first, _, _ = self.action("first", action_class=ActionClass.IRREVERSIBLE)
        second, _, _ = self.action("second")
        with self.assertRaises(ValueError):
            check_bundle((first, second))
