"""Confirmed fake changes run once in the background and report in-channel."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import discord

from elbow_helper.features.agent.actions.contracts import (
    ActionClass, ChangePreview, PreparedAction, check_bundle,
)
from elbow_helper.features.agent.actions.store import AgentActionRepository
from elbow_helper.features.agent.actions.runner import AgentActionRunner, StopActionRunView
from elbow_helper.features.agent.actions.outcomes import ActionOutcome
from elbow_helper.features.agent.actions.preview import preview_text
from elbow_helper.features.agent.actions.private_view import PrivateResultView
from elbow_helper.features.agent.actions.combined_reply import CombinedReplyView
from elbow_helper.features.agent.actions.preview import ConfirmationView
from elbow_helper.features.agent.text import chunk_response


class ActionRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_dm_delivery_keeps_access_checks_in_the_home_channel(self):
        destination = SimpleNamespace(id=9, send=AsyncMock(return_value=self.progress))
        self.context.delivery_channel = destination
        action = self.action("synthetic change", outcome=ActionOutcome("complete"))[0]
        with (
            patch("elbow_helper.features.agent.actions.runner.require_access") as access,
            patch(
                "elbow_helper.features.agent.actions.runner.require_evidence_access", AsyncMock(),
            ),
        ):
            run_id = await self.runner.submit(self.context, (action,), confirmer_id=4)
            await self.runner.wait_run(run_id)
        self.channel.send.assert_not_awaited()
        destination.send.assert_awaited_once()
        self.assertEqual(self.progress.edit.await_args.kwargs["content"], "Done: synthetic change.")
        self.assertTrue(access.called)
        self.assertTrue(all(call.args[2] is self.channel for call in access.call_args_list))

    async def test_combined_confirmation_keeps_dispatch_alive_until_the_run_finishes(self):
        action = self.action("synthetic change", outcome=ActionOutcome("complete"))[0]
        preview = ConfirmationView(4, (action,), self.context, runner=self.runner)
        combined = CombinedReplyView(preview_text([action]), "Synthetic answer", preview, None,
                                     preview_first=True, on_change=AsyncMock())
        self.addCleanup(combined.stop)
        message = SimpleNamespace(id=701, edit=AsyncMock())
        running_controls = []
        async def edit(**kwargs):
            if isinstance(combined.views["preview"], StopActionRunView):
                running_controls.append((combined.views["preview"], combined.is_finished()))
            return message
        message.edit.side_effect = edit
        combined.start(message)
        interaction = SimpleNamespace(user=SimpleNamespace(id=4), response=SimpleNamespace(defer=AsyncMock()))
        with (patch("elbow_helper.features.agent.actions.runner.require_access"),
              patch("elbow_helper.features.agent.actions.runner.require_evidence_access", AsyncMock())):
            await preview.confirm(interaction)
            await asyncio.gather(*tuple(self.runner._tasks))
        self.assertEqual(combined.parts["preview"], "Done: synthetic change.")
        self.assertEqual(combined.parts["answer"], "Synthetic answer")
        self.assertEqual(len(running_controls), 1)
        self.assertFalse(running_controls[0][1])
        self.channel.send.assert_not_awaited()

    async def test_combined_preview_reports_preserve_the_answer_and_private_controls(self):
        actions = [self.action(name, outcome=ActionOutcome("complete"))[0] for name in ("first", "second")]
        preview = ConfirmationView(4, tuple(actions), self.context, runner=self.runner)
        private = PrivateResultView(4, ("Synthetic private lookup",))
        combined = CombinedReplyView(preview_text(actions), "Synthetic answer", preview, private,
                                     preview_first=True, on_change=AsyncMock())
        self.addCleanup(combined.stop)
        message = SimpleNamespace(id=701, edit=AsyncMock(), delete=AsyncMock())
        message.edit.return_value = message
        combined.start(message)
        await self.run_actions(*actions, progress_message=preview._progress_message())
        contents = [call.kwargs["content"] for call in message.edit.await_args_list]
        self.assertEqual(contents, [f"1. {text}\n\n2. Synthetic answer" for text in (
            "Running 2 changes...", "1 of 2 done...", "Done: first, second.",
        )])
        self.assertIs(combined.views["answer"], private)
        self.assertFalse(private.expired)
        self.assertTrue(all(not item.disabled for item in private.children))
        self.channel.send.assert_not_awaited()
        message.delete.assert_not_awaited()

    async def test_combined_posting_run_keeps_the_answer_instead_of_deleting_its_message(self):
        action = self.action("synthetic post", outcome=ActionOutcome("complete", posted_in=2))[0]
        preview = ConfirmationView(4, (action,), self.context, runner=self.runner)
        combined = CombinedReplyView(preview_text([action]), "Synthetic answer", preview, None,
                                     preview_first=False, on_change=AsyncMock())
        self.addCleanup(combined.stop)
        message = SimpleNamespace(id=701, edit=AsyncMock(), delete=AsyncMock())
        message.edit.return_value = message
        combined.start(message)
        await self.run_actions(action, progress_message=preview._progress_message())
        message.delete.assert_not_awaited()
        self.assertEqual(message.edit.await_args.kwargs["content"],
                         "1. Synthetic answer\n\n2. Done: synthetic post.")

    async def test_long_combined_run_report_uses_remaining_room_without_losing_text(self):
        action = self.action("long", allowed=False)[0]
        action = replace(action, preview=replace(action.preview, summary="Synthetic " + "x" * 2500))
        # The initial preview was short; a later run report can grow beyond its part.
        preview = ConfirmationView(4, (self.action("long")[0],), self.context)
        combined = CombinedReplyView("Synthetic preview", "Synthetic answer " + "y" * 1800,
                                     preview, None, preview_first=True, on_change=AsyncMock())
        self.addCleanup(combined.stop)
        message = SimpleNamespace(id=701, edit=AsyncMock())
        message.edit.return_value = message
        combined.start(message)
        await self.run_actions(action, progress_message=preview._progress_message())
        self.assertTrue(all(len(call.kwargs["content"]) <= 2000 for call in message.edit.await_args_list))
        self.assertTrue(all(len(call.args[0]) <= 2000 for call in self.channel.send.await_args_list))
        self.assertEqual(sum(call.args[0].count("x") for call in self.channel.send.await_args_list)
                         + combined.parts["preview"].count("x"), 2500)
        self.assertEqual(combined.parts["answer"], "Synthetic answer " + "y" * 1800)

    async def test_public_parts_follow_the_shared_text_boundaries(self):
        content = "Synthetic line. " * 300 + "\n" + "x" * 2500
        await self.runner._send_parts(self.channel, content)
        self.assertEqual([call.args[0] for call in self.channel.send.await_args_list],
                         chunk_response(content))
        self.assertTrue(all(not call.kwargs["allowed_mentions"].users
                            for call in self.channel.send.await_args_list))

    async def test_permission_failure_names_the_required_permission(self):
        action, _, run = self.action("roles")
        run.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                                            "Synthetic")
        action = replace(action, permission="Manage Roles")
        result = await self.run_actions(action)
        self.assertEqual(result["steps"][0]["status"], "permission")
        self.assertEqual(self.progress.edit.await_args.kwargs["content"],
                         "I'm missing the Manage Roles permission for: roles.")

    async def test_transport_failures_are_unconfirmed(self):
        for error in (TimeoutError("Synthetic"), discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"), "Synthetic",
        )):
            with self.subTest(error=type(error).__name__):
                action, _, run = self.action("first")
                run.side_effect = error
                result = await self.run_actions(action)
                self.assertEqual(result["steps"][0]["status"], "uncertain")
                self.assertEqual(self.progress.edit.await_args.kwargs["content"],
                    "Couldn't confirm: first. Check before asking me to retry.")

    async def test_unrecorded_success_is_never_written_as_failed(self):
        action, _, run = self.action("first")
        with patch.object(self.repository, "finish_step", return_value=False) as finish:
            result = await self.run_actions(action)
        run.assert_awaited_once()
        self.assertEqual(result["steps"][0]["status"], "running")
        self.assertEqual([call.kwargs["status"] for call in finish.call_args_list], ["completed"])
        self.assertEqual(self.progress.edit.await_args.kwargs["content"],
                         "Couldn't confirm: first. Check before asking me to retry.")

    async def test_committed_success_survives_a_recording_exception(self):
        action, _, _ = self.action("first")
        finish = self.repository.finish_step
        def record_then_fail(*args, **kwargs):
            finish(*args, **kwargs)
            raise OSError("Synthetic recording failure")
        with patch.object(self.repository, "finish_step", side_effect=record_then_fail) as recording:
            result = await self.run_actions(action)
        self.assertEqual(result["steps"][0]["status"], "completed")
        self.assertEqual([call.kwargs["status"] for call in recording.call_args_list], ["completed"])
        self.assertEqual(self.repository.recent_log(requester_id=4)[0]["outcome"], "completed")
        self.assertEqual(self.progress.edit.await_args.kwargs["content"],
                         "Couldn't confirm: first. Check before asking me to retry.")

    def test_restart_report_separates_unconfirmed_and_unstarted_steps(self):
        run = {"status": "interrupted", "steps": [
            {"action_label": "Done", "action_class": "change", "status": "completed"},
            {"action_label": "Started", "action_class": "change", "status": "interrupted"},
            {"action_label": "Waiting", "action_class": "change", "status": "queued"},
        ]}
        self.assertEqual(self.runner._report(run),
            "I restarted before finishing these changes.\nDone: Done.\nNot done: Waiting.\n"
            "Couldn't confirm: Started. Check before asking me to retry.")

    def test_report_groups_labels_and_each_missing_permission(self):
        def step(label, status, permission=""):
            return {"action_label": label, "action_class": "change", "status": status,
                    "outcome_json": json.dumps({"permission": permission}),
                    "preview_json": json.dumps(["Synthetic private detail"])}
        report = self.runner._report({"status": "failed", "steps": [
            step("Roles", "completed"), step("Posts", "completed"),
            step("Roles", "completed"), step("Waiting", "queued"),
            step("Grant", "permission", "Manage Roles"),
            step("Post", "permission", "Send Messages"),
            step("Grant", "permission", "Manage Roles"),
            step("Started", "uncertain"),
        ]})
        self.assertEqual(report,
            "Done: Roles (2), Posts.\nNot done: Waiting.\n"
            "I'm missing the Manage Roles permission for: Grant (2).\n"
            "I'm missing the Send Messages permission for: Post.\n"
            "Couldn't confirm: Started. Check before asking me to retry.")
        self.assertNotIn("Synthetic private detail", report)

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

    async def run_actions(self, *actions, progress_message=None):
        with (patch("elbow_helper.features.agent.actions.runner.require_access"),
              patch("elbow_helper.features.agent.actions.runner.require_evidence_access",
                    new_callable=AsyncMock)):
            run_id = await self.runner.submit(
                self.context, tuple(actions), confirmer_id=self.context.member.id,
                progress_message=progress_message,
            )
            await asyncio.gather(*tuple(self.runner._tasks))
        return self.repository.run(run_id)

    def action(self, name, *, outcome=None, allowed=True,
               action_class=ActionClass.CHANGE, verify=None):
        check = AsyncMock(return_value=allowed)
        run = AsyncMock(return_value=outcome or ActionOutcome("complete", text=name))
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
        self.assertEqual("Done: first, second.",
                         self.progress.edit.await_args.kwargs["content"])
        self.assertEqual(len(self.repository.recent_log(requester_id=4)), 2)

    async def test_wait_run_returns_the_finished_action_record(self):
        action, _, _ = self.action("first")
        with (patch("elbow_helper.features.agent.actions.runner.require_access"),
              patch("elbow_helper.features.agent.actions.runner.require_evidence_access",
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
        self.assertEqual("Nothing changed. Not done: first, second.",
                         self.progress.edit.await_args.kwargs["content"])

    async def test_adjacent_actions_share_one_report_group(self):
        actions = []
        for name in ("one", "two", "three"):
            action, _, _ = self.action(name, outcome=ActionOutcome("complete"))
            actions.append(replace(action, preview=replace(
                action.preview, summary="Add role",
            )))
        await self.run_actions(*actions)
        self.assertEqual("Done: Add role (3).",
                         self.progress.edit.await_args.kwargs["content"])
        self.channel.send.assert_awaited_once()

    async def test_confirmed_preview_shows_progress_and_the_report(self):
        first, _, _ = self.action("first", outcome=ActionOutcome("complete"))
        second, _, _ = self.action("second", outcome=ActionOutcome("complete"))
        preview = SimpleNamespace(edit=AsyncMock())
        await self.run_actions(first, second, progress_message=preview)
        self.assertEqual([call.kwargs["content"] for call in preview.edit.await_args_list],
                         ["Running 2 changes...", "1 of 2 done...", "Done: first, second."])
        self.channel.send.assert_not_awaited()

    async def test_progress_gets_its_own_message_when_the_preview_cannot_change(self):
        action, _, _ = self.action("first", outcome=ActionOutcome("complete"))
        preview = SimpleNamespace(edit=AsyncMock(side_effect=discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"), "Synthetic",
        )))
        with self.assertLogs("elbow_helper.features.agent.actions.runner", level="WARNING"):
            await self.run_actions(action, progress_message=preview)
        self.assertEqual(self.channel.send.await_args.args[0], "Running 1 change...")
        self.assertEqual(self.progress.edit.await_args.kwargs["content"], "Done: first.")

    async def test_posts_in_the_run_channel_leave_no_report(self):
        actions = [self.action(name, outcome=ActionOutcome(
            "complete", posted_in=self.channel.id,
        ))[0] for name in ("first", "second")]
        preview = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        self.runner.on_finish = AsyncMock()
        await self.run_actions(*actions, progress_message=preview)
        preview.delete.assert_awaited_once()
        self.assertEqual([call.kwargs["content"] for call in preview.edit.await_args_list],
                         ["Running 2 changes...", "1 of 2 done..."])
        self.channel.send.assert_not_awaited()
        self.assertIsNone(self.runner.on_finish.await_args.args[2])

    async def test_other_runs_keep_their_report(self):
        def post(channel_id, *, allowed=True, **options):
            return self.action("post", outcome=ActionOutcome(
                "complete", posted_in=channel_id, **options,
            ), allowed=allowed)[0]
        role, _, _ = self.action("role", outcome=ActionOutcome("complete"))
        cases = (
            ("Done: post.", (post(9),)),
            ("Done: post, role.", (post(self.channel.id), role)),
            ("Nothing changed. Not done: post.", (post(self.channel.id, allowed=False),)),
            ("Done: post.", (post(self.channel.id, visibility="private",
                                  private_parts=("Synthetic private result",)),)),
        )
        for report, actions in cases:
            with self.subTest(report=report, steps=len(actions)):
                preview = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
                await self.run_actions(*actions, progress_message=preview)
                preview.delete.assert_not_awaited()
                self.assertEqual(preview.edit.await_args.kwargs["content"], report)

    async def test_report_stays_when_the_progress_cannot_be_removed(self):
        action, _, _ = self.action("post", outcome=ActionOutcome(
            "complete", posted_in=self.channel.id,
        ))
        preview = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock(
            side_effect=discord.HTTPException(
                SimpleNamespace(status=503, reason="Unavailable"), "Synthetic",
            ),
        ))
        with self.assertLogs("elbow_helper.features.agent.actions.runner", level="WARNING"):
            await self.run_actions(action, progress_message=preview)
        self.assertEqual(preview.edit.await_args.kwargs["content"], "Done: post.")

    async def test_long_remaining_lines_split_after_replacing_progress(self):
        action, _, _ = self.action("long", allowed=False)
        action = replace(action, preview=replace(
            action.preview, summary="Not done: " + "x" * 2500,
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
            return ActionOutcome("complete", text="first")
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
        action, _, _ = self.action("private", outcome=ActionOutcome(
            "complete", "private", private_parts=("secret",),
        ))
        await self.run_actions(action)
        view = self.progress.edit.await_args.kwargs["view"]
        self.assertIsInstance(view, PrivateResultView)
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
        first, _, first_run = self.action("first", outcome=ActionOutcome(
            "complete", result={"target_id": 7},
        ))
        first = replace(first, step_id="created")
        second_run = AsyncMock(return_value=ActionOutcome("complete"))
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
            return ActionOutcome("complete", after={"value": "new"})
        run.side_effect = apply
        await self.run_actions(action)
        entry = self.repository.recent_log(requester_id=4)[0]

        async def undo_handler(context, log):
            async def execute_undo():
                state["value"] = log["before"]["value"]
                return ActionOutcome("complete", after=dict(state))
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
