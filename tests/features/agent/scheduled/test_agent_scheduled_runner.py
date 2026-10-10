"""Scheduled work claims once and retains watcher state."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import discord

from elbow_helper.features.agent.actions.store import AgentActionRepository
from elbow_helper.features.agent.actions.contracts import ChangePreview, PreparedAction
from elbow_helper.features.agent.actions.outcomes import ActionOutcome
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.models import AgentRequestContext, AgentTurnState
from elbow_helper.features.agent.scheduled.runner import ScheduledRunner
from elbow_helper.features.agent.engine.service import AgentUnavailableError
from elbow_helper.features.agent.scheduled.requests import (
    ScheduledMessage,
    ScheduledResult,
    check_watcher,
    run_saved_request,
)
from elbow_helper.features.agent.scheduled.watchers import comparison_data, evaluate, send_alert
from elbow_helper.infrastructure.ai.agent import (
    AgentStep,
    AgentUsage,
    AgentReasoningEffort,
)


class ScheduledRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fenced_watcher_json_is_accepted(self):
        for fence in ("```", "```json"):
            session = SimpleNamespace(advance=AsyncMock(return_value=AgentStep(
                fence + '\n{"holds":true,"alert":"It changed."}\n```', (), AgentUsage())))
            context = SimpleNamespace(deadline_monotonic=None,
                bot=SimpleNamespace(agent_model=SimpleNamespace(create_agent_session=lambda **_: session)))
            self.assertEqual(await evaluate(context, "Has changed", []), (True, "It changed."))

    def _due_once(self, kind="request"):
        return self.repository.create_standing(
            kind=kind, guild_id=1, requester_id=2, destination_channel_id=3,
            rule={"request": "Synthetic", "schedule": {
                "kind": "once", "at_utc": "2026-01-01T00:00:00Z"}}, next_at=1,
        )

    async def test_service_outage_keeps_rules_active_and_notifies_once(self):
        for kind, operation in (("request", self.run_request), ("watcher", self.watch_request)):
            with self.subTest(kind=kind):
                self.channel.send.reset_mock()
                identifier = self._due_once(kind)
                operation.side_effect = AgentUnavailableError("Synthetic")
                await self._tick()
                await self._tick()
                row = self.repository.standing(kind=kind, identifier=identifier)
                self.assertEqual(row["status"], "active")
                self.assertIsNone(row["lease_owner"])
                self.assertEqual(row["notice_sent"], 1)
                self.channel.send.assert_awaited_once()
                notice = self.channel.send.await_args
                self.assertNotIn("<@", notice.args[0])
                self.assertFalse(notice.kwargs["allowed_mentions"].users)
                operation.side_effect = None
                await self._tick()
                row = self.repository.standing(kind=kind, identifier=identifier)
                self.assertEqual(row["status"], "completed")
                self.assertEqual(row["notice_sent"], 0)
                table = "saved_requests" if kind == "request" else "watchers"
                due = "next_run_at" if kind == "request" else "next_check_at"
                key = "request_id" if kind == "request" else "watcher_id"
                with self.repository.connect() as connection:
                    connection.execute(f"UPDATE {table} SET status='active', {due}=1 WHERE {key}=?",
                                       (identifier,))
                    connection.commit()
                operation.side_effect = AgentUnavailableError("Synthetic next outage")
                await self._tick()
                await self._tick()
                self.assertEqual(self.channel.send.await_count, 2)
                operation.side_effect = None
                await self._tick()

    async def test_transient_destination_failures_leave_the_run_due(self):
        self.member.guild.get_channel_or_thread = lambda _: None
        self.channel.guild = self.member.guild
        self.bot.fetch_channel = AsyncMock(return_value=self.channel)
        self.runner._destination = lambda rule: ScheduledRunner._destination(self.runner, rule)
        for error in (TimeoutError("Synthetic"), discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"), "Synthetic",
        )):
            with self.subTest(error=type(error).__name__):
                identifier = self._due_once()
                self.bot.fetch_channel.side_effect = error
                await self._tick()
                row = self.repository.standing(kind="request", identifier=identifier)
                self.assertEqual(row["status"], "active")
                self.assertEqual(row["next_run_at"], 1)
                self.assertIsNone(row["lease_owner"])
                self.bot.fetch_channel.side_effect = None
                await self._tick()
                self.assertEqual(self.repository.standing(
                    kind="request", identifier=identifier)["status"], "completed")

    async def test_missing_or_forbidden_destination_pauses_the_rule(self):
        self.member.guild.get_channel_or_thread = lambda _: None
        self.bot.fetch_channel = AsyncMock()
        self.runner._destination = lambda rule: ScheduledRunner._destination(self.runner, rule)
        for error_type, status in ((discord.NotFound, 404), (discord.Forbidden, 403)):
            with self.subTest(status=status):
                identifier = self._due_once()
                self.bot.fetch_channel.side_effect = error_type(
                    SimpleNamespace(status=status, reason="Synthetic"), "Synthetic")
                await self._tick()
                row = self.repository.standing(kind="request", identifier=identifier)
                self.assertEqual(row["status"], "paused")
                self.assertIsNone(row["lease_owner"])
        self.run_request.assert_not_awaited()

    async def test_unexpected_task_failure_releases_and_pauses_once(self):
        identifier = self._due_once()
        self.runner.context_factory = lambda *_: (_ for _ in ()).throw(LookupError("Synthetic"))
        await self._tick()
        await self._tick()
        row = self.repository.standing(kind="request", identifier=identifier)
        self.assertEqual(row["status"], "paused")
        self.assertIsNone(row["lease_owner"])
        self.channel.send.assert_awaited_once()

    async def test_poll_continues_after_an_unexpected_exception(self):
        self.runner.tick = AsyncMock(side_effect=[LookupError("Synthetic"), asyncio.CancelledError()])
        with patch("elbow_helper.features.agent.scheduled.runner.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(asyncio.CancelledError):
                await self.runner._main()
        self.assertEqual(self.runner.tick.await_count, 2)

    async def test_watcher_alert_chunks_and_mentions_only_the_first_part(self):
        alert = "Synthetic alert. " * 300
        context = SimpleNamespace(member=self.member, source_message=SimpleNamespace(channel=self.channel))
        await send_alert(context, alert)
        calls = self.channel.send.await_args_list
        self.assertGreater(len(calls), 1)
        self.assertTrue(calls[0].args[0].startswith("<@2> "))
        self.assertTrue(all(len(call.args[0]) <= 2000 for call in calls))
        self.assertTrue(all("<@2>" not in call.args[0] for call in calls[1:]))
        self.assertTrue(all(not call.kwargs["allowed_mentions"].users for call in calls[1:]))

    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = AgentActionRepository(
            Path(directory.name) / "actions.sqlite3"
        )
        self.member = SimpleNamespace(
            id=2, guild=SimpleNamespace(id=1, me=object()), mention="<@2>"
        )
        self.member.guild.get_member = lambda _: self.member
        self.channel = SimpleNamespace(id=3, send=AsyncMock())
        self.channel.send.return_value = SimpleNamespace(id=4)
        self.bot = SimpleNamespace(get_guild=lambda _: self.member.guild)
        self.run_request = AsyncMock(return_value=ScheduledResult())
        self.watch_request = AsyncMock(return_value=ScheduledResult())
        context_factory = lambda message, member: SimpleNamespace(
            source_message=message,
            member=member,
            state=AgentTurnState(),
        )
        for name, function in (
            ("run_saved_request", self.run_request),
            ("check_watcher", self.watch_request),
        ):
            collaborator = patch(
                "elbow_helper.features.agent.scheduled.runner." + name,
                function,
            )
            collaborator.start()
            self.addCleanup(collaborator.stop)
        self.runner = ScheduledRunner(
            bot=self.bot,
            repository=self.repository,
            guild_id=1,
            service=SimpleNamespace(),
            delivery=AsyncMock(),
            action_runner=SimpleNamespace(),
            context_factory=context_factory,
        )
        self.runner._destination = AsyncMock(return_value=self.channel)
        self.access = patch(
            "elbow_helper.features.agent.scheduled.runner.require_access",
            return_value=self.member,
        )
        self.post = patch(
            "elbow_helper.features.agent.scheduled.runner.check_post_access"
        )
        self.access.start()
        self.post.start()
        self.addCleanup(self.access.stop)
        self.addCleanup(self.post.stop)

    async def _tick(self):
        await self.runner.tick()
        await asyncio.gather(*tuple(self.runner._tasks))

    async def test_missed_once_runs_once(self):
        identifier = self.repository.create_standing(
            kind="request",
            guild_id=1,
            requester_id=2,
            destination_channel_id=3,
            rule={
                "request": "Check status",
                "schedule": {"kind": "once", "at_utc": "2026-01-01T00:00:00Z"},
            },
            next_at=1,
        )
        await self._tick()
        await self._tick()
        self.run_request.assert_awaited_once()
        self.assertEqual(
            self.repository.standing(kind="request", identifier=identifier)["status"],
            "completed",
        )

    async def test_watcher_completes_after_first_alert(self):
        identifier = self.repository.create_standing(
            kind="watcher",
            guild_id=1,
            requester_id=2,
            destination_channel_id=3,
            rule={
                "request": "Watch status",
                "schedule": {"kind": "once", "at_utc": "2026-01-01T00:00:00Z"},
            },
            next_at=1,
        )
        self.watch_request.return_value = ScheduledResult(
            last_result=[{"value": 1}], holding=True, completed=True
        )
        await self._tick()
        row = self.repository.standing(kind="watcher", identifier=identifier)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["last_result"], [{"value": 1}])
        self.assertEqual(row["holding"], 1)

    async def test_same_rule_does_not_overlap(self):
        gate = asyncio.Event()

        async def wait_for_gate(*_, **kwargs):
            await gate.wait()

        self.run_request.side_effect = wait_for_gate
        self.repository.create_standing(
            kind="request",
            guild_id=1,
            requester_id=2,
            destination_channel_id=3,
            rule={
                "request": "Check status",
                "schedule": {"kind": "once", "at_utc": "2026-01-01T00:00:00Z"},
            },
            next_at=1,
        )
        await self.runner.tick()
        await self.runner.tick()
        self.assertEqual(len(self.runner._tasks), 1)
        gate.set()
        await asyncio.gather(*tuple(self.runner._tasks))
        self.run_request.assert_awaited_once()

    async def test_access_loss_pauses_and_reports(self):
        identifier = self.repository.create_standing(
            kind="request",
            guild_id=1,
            requester_id=2,
            destination_channel_id=3,
            rule={
                "request": "Check status",
                "schedule": {"kind": "once", "at_utc": "2026-01-01T00:00:00Z"},
            },
            next_at=1,
        )
        with patch(
            "elbow_helper.features.agent.scheduled.runner.require_access",
            side_effect=ValueError("gone"),
        ):
            await self._tick()
        self.assertEqual(
            self.repository.standing(kind="request", identifier=identifier)["status"],
            "paused",
        )
        self.run_request.assert_not_awaited()
        self.channel.send.assert_awaited_once()

    async def test_changed_decision_uses_low_effort(self):
        session = SimpleNamespace(
            advance=AsyncMock(
                return_value=AgentStep(
                    '{"holds":true,"alert":"It changed."}',
                    (),
                    AgentUsage(100, 20),
                )
            )
        )
        context = SimpleNamespace(
            deadline_monotonic=None,
            state=AgentTurnState(),
            bot=SimpleNamespace(
                agent_model=SimpleNamespace(create_agent_session=lambda **_: session)
            ),
        )
        result = await evaluate(context, "Has changed", [{"value": 1}])
        self.assertEqual(result, (True, "It changed."))
        self.assertEqual(
            session.advance.await_args.kwargs["reasoning_effort"],
            AgentReasoningEffort.LOW,
        )

    async def test_cutoff_continuation_completes_the_decision(self):
        session = SimpleNamespace(
            advance=AsyncMock(
                side_effect=[
                    AgentStep('{"holds":', (), AgentUsage(), output_limit_reached=True),
                    AgentStep('true,"alert":"It changed."}', (), AgentUsage()),
                ]
            )
        )
        context = SimpleNamespace(
            deadline_monotonic=None,
            state=AgentTurnState(),
            bot=SimpleNamespace(
                agent_model=SimpleNamespace(create_agent_session=lambda **_: session)
            ),
        )
        result = await evaluate(context, "Has changed", [{"value": 1}])
        self.assertEqual(result, (True, "It changed."))
        self.assertEqual(session.advance.await_count, 2)
        self.assertFalse(session.advance.await_args.kwargs["allow_tools"])
        self.assertEqual(
            session.advance.await_args.kwargs["continuation_instruction"],
            "Finish the JSON object.",
        )

    def test_lookup_metadata_does_not_change_watched_facts(self):
        first = {
            "report_id": "first",
            "observed_at": "earlier",
            "members": [{"id": 2, "created_at": "source-time"}],
        }
        second = {**first, "report_id": "second", "observed_at": "later"}
        self.assertEqual(comparison_data(first), comparison_data(second))
        changed = {**second, "members": [{"id": 3, "created_at": "source-time"}]}
        self.assertNotEqual(comparison_data(first), comparison_data(changed))
        self.assertEqual(
            comparison_data(first)["members"][0]["created_at"], "source-time"
        )

    async def test_unchanged_results_do_not_call_model(self):
        context = SimpleNamespace(member=SimpleNamespace(mention="<@2>"))
        saved = {
            "rule": {"reads": [], "condition": "Has changed", "repeat": True},
            "last_result": [{"value": 1}],
            "holding": True,
        }
        with (
            patch(
                "elbow_helper.features.agent.scheduled.watchers.read_current",
                AsyncMock(return_value=[{"value": 1}]),
            ) as read,
            patch(
                "elbow_helper.features.agent.scheduled.watchers.evaluate", AsyncMock()
            ) as evaluate,
        ):
            result = await check_watcher(context, saved)
        read.assert_awaited_once()
        evaluate.assert_not_awaited()
        self.assertEqual(
            result, ScheduledResult(last_result=[{"value": 1}], holding=True)
        )

    async def test_alert_only_on_rising_edge(self):
        context = SimpleNamespace(member=SimpleNamespace(mention="<@2>"))
        saved = {
            "rule": {"reads": [], "condition": "Has changed", "repeat": True},
            "last_result": [{"value": 1}],
            "holding": True,
        }
        with (
            patch(
                "elbow_helper.features.agent.scheduled.watchers.read_current",
                AsyncMock(return_value=[{"value": 2}]),
            ),
            patch(
                "elbow_helper.features.agent.scheduled.watchers.evaluate",
                AsyncMock(return_value=(True, "Alert")),
            ),
            patch(
                "elbow_helper.features.agent.scheduled.watchers.send_alert", AsyncMock()
            ) as send,
        ):
            result = await check_watcher(context, saved)
        send.assert_not_awaited()
        self.assertEqual(
            result, ScheduledResult(last_result=[{"value": 2}], holding=True)
        )

    async def test_scheduled_channel_reply_has_no_unseen_archive_request(self):
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=10)))
        message = ScheduledMessage(
            guild=SimpleNamespace(id=1),
            channel=channel,
            author=SimpleNamespace(id=2),
            content="Check status",
        )
        delivery = AgentDeliveryMixin()
        delivery.bot = SimpleNamespace(user=None)
        delivery._archive_reply = AsyncMock()
        await delivery.send_response(message, "Status checked", None)
        channel.send.assert_awaited_once()
        delivery._archive_reply.assert_not_awaited()


class SavedRequestScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_scope_change_preserves_a_read_answer_without_output_steps(self):
        proposal = PreparedAction(
            "synthetic_change", {"target": 321},
            ChangePreview(("Change synthetic target",), AsyncMock(return_value=True)), AsyncMock(),
            step_id="change", capability_name="synthetic_change", checked_arguments={"target": 321},
        )
        state = AgentTurnState(proposed_changes=[proposal], preview_reply="Synthetic preview")
        context = AgentRequestContext(
            bot=None, guild=None, member=SimpleNamespace(id=121), source_message=object(),
            account_links=None, message_search=None, state=state,
        )
        service = SimpleNamespace(answer=AsyncMock(return_value="Synthetic lookup answer"))
        runner = SimpleNamespace(submit=AsyncMock(return_value="synthetic-run"),
                                 wait_run=AsyncMock(return_value={"status": "completed"}))
        delivery = AsyncMock()
        with patch("elbow_helper.features.agent.scheduled.requests.within_scope", return_value=True):
            result = await run_saved_request(context, {"request": "Synthetic mixed request"},
                                            service=service, action_runner=runner, delivery=delivery)
        self.assertEqual(result.action_run["status"], "completed")
        self.assertEqual(delivery.await_args.args[1], "Synthetic lookup answer")
        self.assertEqual(delivery.await_args.kwargs["context"].state.proposed_changes, [])
        self.assertIsNone(delivery.await_args.kwargs["context"].state.preview_reply)

    async def test_in_scope_action_also_delivers_output_steps(self):
        async def recheck():
            return True

        async def run():
            return None

        proposal = PreparedAction(
            "add_discord_roles",
            {"role_id": 12, "member_ids": [21]},
            ChangePreview(("Add role",), recheck),
            run,
            step_id="roles", capability_name="add_discord_roles",
            checked_arguments={"role_id": 12, "member_ids": [21]},
        )
        state = AgentTurnState(
            outcomes=[
                ActionOutcome("complete", "public", text="Roster posted."),
            ]
        )
        context = AgentRequestContext(
            bot=None,
            guild=None,
            member=SimpleNamespace(id=2),
            source_message=object(),
            account_links=None,
            message_search=None,
            state=state,
        )
        service = SimpleNamespace(
            answer=AsyncMock(
                side_effect=lambda **_: (
                    state.proposed_changes.append(proposal) or "Preview"
                )
            )
        )
        runner = SimpleNamespace(
            submit=AsyncMock(return_value="run-1"),
            wait_run=AsyncMock(return_value={"status": "completed"}),
        )
        agent = SimpleNamespace(
            service=service, action_runner=runner, send_response=AsyncMock()
        )
        rule = {
            "request": "Do both",
            "allowed_actions": [
                {
                    "capability": "add_discord_roles",
                    "fixed_values": {"role_id": 12},
                    "variable_fields": ["member_ids"],
                    "max_targets": 2,
                }
            ],
        }
        await run_saved_request(
            context,
            rule,
            service=agent.service,
            action_runner=agent.action_runner,
            delivery=agent.send_response,
        )
        agent.send_response.assert_awaited_once()
        self.assertEqual(agent.send_response.await_args.args[1], "Roster posted.")
        self.assertEqual(
            agent.send_response.await_args.kwargs["context"].state.proposed_changes, []
        )

    async def test_confirmed_scope_runs_without_a_second_confirmation(self):
        async def recheck():
            return True

        async def run():
            return None

        proposal = PreparedAction(
            "add_discord_roles",
            {"role_id": 12, "member_ids": [21]},
            ChangePreview(("Add role",), recheck),
            run,
            step_id="roles", capability_name="add_discord_roles",
            checked_arguments={"role_id": 12, "member_ids": [21]},
        )
        state = SimpleNamespace(
            proposed_changes=[], outcomes=[], attachments=[], preview_reply=None
        )
        context = SimpleNamespace(
            source_message=object(), state=state, member=SimpleNamespace(id=2)
        )
        service = SimpleNamespace(
            answer=AsyncMock(
                side_effect=lambda **_: (
                    state.proposed_changes.append(proposal) or "Preview"
                )
            )
        )
        action_runner = SimpleNamespace(
            submit=AsyncMock(return_value="run-1"),
            wait_run=AsyncMock(return_value={"status": "completed"}),
        )
        agent = SimpleNamespace(
            service=service, action_runner=action_runner, send_response=AsyncMock()
        )
        rule = {
            "request": "Give the role",
            "allowed_actions": [
                {
                    "capability": "add_discord_roles",
                    "fixed_values": {"role_id": 12},
                    "variable_fields": ["member_ids"],
                    "max_targets": 2,
                }
            ],
        }
        await run_saved_request(
            context,
            rule,
            service=agent.service,
            action_runner=agent.action_runner,
            delivery=agent.send_response,
        )
        action_runner.submit.assert_awaited_once()
        agent.send_response.assert_not_awaited()

    async def test_action_outside_confirmed_scope_gets_normal_preview(self):
        async def recheck():
            return True

        async def run():
            return None

        proposal = PreparedAction(
            "add_discord_roles",
            {"role_id": 13, "member_ids": [21]},
            ChangePreview(("Add role",), recheck),
            run,
            step_id="roles", capability_name="add_discord_roles",
            checked_arguments={"role_id": 13, "member_ids": [21]},
        )
        state = SimpleNamespace(
            proposed_changes=[], outcomes=[], attachments=[], preview_reply=None
        )
        context = SimpleNamespace(
            source_message=object(), state=state, member=SimpleNamespace(id=2)
        )
        service = SimpleNamespace(
            answer=AsyncMock(
                side_effect=lambda **_: (
                    state.proposed_changes.append(proposal) or "Preview"
                )
            )
        )
        action_runner = SimpleNamespace(submit=AsyncMock())
        agent = SimpleNamespace(
            service=service, action_runner=action_runner, send_response=AsyncMock()
        )
        rule = {
            "request": "Give the role",
            "allowed_actions": [
                {
                    "capability": "add_discord_roles",
                    "fixed_values": {"role_id": 12},
                    "variable_fields": ["member_ids"],
                    "max_targets": 2,
                }
            ],
        }
        await run_saved_request(
            context,
            rule,
            service=agent.service,
            action_runner=agent.action_runner,
            delivery=agent.send_response,
        )
        action_runner.submit.assert_not_awaited()
        agent.send_response.assert_awaited_once()
        self.assertEqual(agent.send_response.await_args.kwargs["preview_timeout"], 3600.0)
        self.assertTrue(agent.send_response.await_args.kwargs["mention_requester"])
