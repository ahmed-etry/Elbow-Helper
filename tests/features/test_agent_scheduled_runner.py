"""Scheduled work claims once and retains watcher state."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.repository import AgentActionRepository
from elbow_helper.features.agent.actions.contracts import ChangePreview, PreparedAction
from elbow_helper.features.agent.cog import AgentCog
from elbow_helper.features.agent.commands.outcomes import CommandOutcome
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.models import AgentRequestContext, AgentTurnState
from elbow_helper.features.agent.scheduled.runner import ScheduledMessage, ScheduledRunner
from elbow_helper.features.agent.scheduled.watchers import check_watcher, _comparison_data
from elbow_helper.features.agent.scheduled.watchers import _evaluate
from elbow_helper.infrastructure.ai.agent import AgentStep, AgentUsage, AgentReasoningEffort


class ScheduledRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fenced_watcher_json_is_accepted(self):
        for fence in ("```", "```json"):
            session = SimpleNamespace(advance=AsyncMock(return_value=AgentStep(
                fence + '\n{"holds":true,"alert":"It changed."}\n```', (), AgentUsage())))
            context = SimpleNamespace(deadline_monotonic=None,
                bot=SimpleNamespace(agent_model=SimpleNamespace(create_agent_session=lambda **_: session)))
            self.assertEqual(await evaluate(context, "Has changed", []), (True, "It changed."))

    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = AgentActionRepository(Path(directory.name) / "actions.sqlite3")
        self.member = SimpleNamespace(id=2, guild=SimpleNamespace(id=1, me=object()),
                                      mention="<@2>")
        self.member.guild.get_member = lambda _: self.member
        self.channel = SimpleNamespace(id=3, send=AsyncMock())
        self.channel.send.return_value = SimpleNamespace(id=4)
        self.bot = SimpleNamespace(get_guild=lambda _: self.member.guild)
        self.agent = SimpleNamespace(
            scheduled_context=lambda message, member: SimpleNamespace(
                source_message=message, member=member,
                state=AgentTurnState()),
            run_saved_request=AsyncMock(), check_watcher=AsyncMock(),
        )
        self.runner = ScheduledRunner(
            bot=self.bot, repository=self.repository, guild_id=1,
            agent=self.agent, enabled=True,
        )
        self.runner._destination = AsyncMock(return_value=self.channel)
        self.access = patch(
            "elbow_helper.features.agent.scheduled.runner.require_access",
            return_value=self.member,
        )
        self.post = patch("elbow_helper.features.agent.scheduled.runner.check_post_access")
        self.access.start()
        self.post.start()
        self.addCleanup(self.access.stop)
        self.addCleanup(self.post.stop)

    async def _tick(self):
        await self.runner.tick()
        await asyncio.gather(*tuple(self.runner._tasks))

    async def test_missed_once_runs_once(self):
        identifier = self.repository.create_standing(
            kind="request", guild_id=1, requester_id=2,
            destination_channel_id=3,
            rule={"request": "Check status", "schedule": {
                "kind": "once", "at_utc": "2026-01-01T00:00:00Z"}},
            next_at=1,
        )
        await self._tick()
        await self._tick()
        self.agent.run_saved_request.assert_awaited_once()
        self.assertEqual(self.repository.standing(kind="request", identifier=identifier)["status"],
                         "completed")

    async def test_watcher_completes_after_first_alert(self):
        identifier = self.repository.create_standing(
            kind="watcher", guild_id=1, requester_id=2,
            destination_channel_id=3,
            rule={"request": "Watch status", "schedule": {
                "kind": "once", "at_utc": "2026-01-01T00:00:00Z"}},
            next_at=1,
        )
        self.agent.check_watcher.return_value = ([{"value": 1}], True, True)
        await self._tick()
        row = self.repository.standing(kind="watcher", identifier=identifier)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["last_result"], [{"value": 1}])
        self.assertEqual(row["holding"], 1)

    async def test_same_rule_does_not_overlap(self):
        gate = asyncio.Event()

        async def wait_for_gate(*_):
            await gate.wait()

        self.agent.run_saved_request.side_effect = wait_for_gate
        self.repository.create_standing(
            kind="request", guild_id=1, requester_id=2,
            destination_channel_id=3,
            rule={"request": "Check status", "schedule": {
                "kind": "once", "at_utc": "2026-01-01T00:00:00Z"}},
            next_at=1,
        )
        await self.runner.tick()
        await self.runner.tick()
        self.assertEqual(len(self.runner._tasks), 1)
        gate.set()
        await asyncio.gather(*tuple(self.runner._tasks))
        self.agent.run_saved_request.assert_awaited_once()

    async def test_access_loss_pauses_and_reports(self):
        identifier = self.repository.create_standing(
            kind="request", guild_id=1, requester_id=2,
            destination_channel_id=3,
            rule={"request": "Check status", "schedule": {
                "kind": "once", "at_utc": "2026-01-01T00:00:00Z"}},
            next_at=1,
        )
        with patch("elbow_helper.features.agent.scheduled.runner.require_access",
                   side_effect=ValueError("gone")):
            await self._tick()
        self.assertEqual(self.repository.standing(kind="request", identifier=identifier)["status"],
                         "paused")
        self.agent.run_saved_request.assert_not_awaited()
        self.channel.send.assert_awaited_once()

    async def test_emergency_switch_leaves_due_rules_untouched(self):
        self.runner.enabled = False
        request_id = self.repository.create_standing(
            kind="request", guild_id=1, requester_id=2,
            destination_channel_id=3,
            rule={"request": "Change a role", "allowed_actions": [{"capability": "role"}],
                  "schedule": {"kind": "once", "at_utc": "2026-01-01T00:00:00Z"}},
            next_at=1,
        )
        watcher_id = self.repository.create_standing(
            kind="watcher", guild_id=1, requester_id=2,
            destination_channel_id=3,
            rule={"request": "Watch status", "schedule": {
                "kind": "once", "at_utc": "2026-01-01T00:00:00Z"}},
            next_at=1,
        )
        request = self.repository.standing(kind="request", identifier=request_id)
        watcher = self.repository.standing(kind="watcher", identifier=watcher_id)
        self.runner.start()
        await self._tick()
        self.agent.run_saved_request.assert_not_awaited()
        self.agent.check_watcher.assert_not_awaited()
        self.runner._destination.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        self.assertFalse(self.runner._tasks)
        self.assertEqual(self.repository.standing(kind="request", identifier=request_id), request)
        self.assertEqual(self.repository.standing(kind="watcher", identifier=watcher_id), watcher)
        self.assertEqual(request["status"], "active")
        self.assertEqual(watcher["status"], "active")


class WatcherDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_decision_uses_low_effort(self):
        session = SimpleNamespace(advance=AsyncMock(return_value=AgentStep(
            '{"holds":true,"alert":"It changed."}', (), AgentUsage(100, 20),
        )))
        context = SimpleNamespace(
            deadline_monotonic=None,
            state=AgentTurnState(),
            bot=SimpleNamespace(agent_model=SimpleNamespace(
                create_agent_session=lambda **_: session)),
        )
        result = await _evaluate(context, "Has changed", [{"value": 1}])
        self.assertEqual(result, (True, "It changed."))
        self.assertEqual(session.advance.await_args.kwargs["reasoning_effort"],
                         AgentReasoningEffort.LOW)

    async def test_cutoff_continuation_completes_the_decision(self):
        session = SimpleNamespace(advance=AsyncMock(side_effect=[
            AgentStep('{"holds":', (), AgentUsage(), output_limit_reached=True),
            AgentStep('true,"alert":"It changed."}', (), AgentUsage()),
        ]))
        context = SimpleNamespace(
            deadline_monotonic=None,
            state=AgentTurnState(),
            bot=SimpleNamespace(agent_model=SimpleNamespace(
                create_agent_session=lambda **_: session)),
        )
        result = await _evaluate(context, "Has changed", [{"value": 1}])
        self.assertEqual(result, (True, "It changed."))
        self.assertEqual(session.advance.await_count, 2)
        self.assertFalse(session.advance.await_args.kwargs["allow_tools"])
        self.assertEqual(session.advance.await_args.kwargs["continuation_instruction"],
                         "Finish the JSON object.")

    def test_lookup_metadata_does_not_change_watched_facts(self):
        first = {"report_id": "first", "observed_at": "earlier",
                 "members": [{"id": 2, "created_at": "source-time"}]}
        second = {**first, "report_id": "second", "observed_at": "later"}
        self.assertEqual(_comparison_data(first), _comparison_data(second))
        changed = {**second, "members": [{"id": 3, "created_at": "source-time"}]}
        self.assertNotEqual(_comparison_data(first), _comparison_data(changed))
        self.assertEqual(_comparison_data(first)["members"][0]["created_at"], "source-time")

    async def test_unchanged_results_do_not_call_model(self):
        context = SimpleNamespace(member=SimpleNamespace(mention="<@2>"))
        saved = {"rule": {"reads": [], "condition": "Has changed", "repeat": True},
                 "last_result": [{"value": 1}], "holding": True}
        with patch("elbow_helper.features.agent.scheduled.watchers._read_current",
                   AsyncMock(return_value=[{"value": 1}])) as read, patch(
                   "elbow_helper.features.agent.scheduled.watchers._evaluate",
                   AsyncMock()) as evaluate:
            result = await check_watcher(context, saved)
        read.assert_awaited_once()
        evaluate.assert_not_awaited()
        self.assertEqual(result, ([{"value": 1}], True, False))

    async def test_alert_only_on_rising_edge(self):
        context = SimpleNamespace(member=SimpleNamespace(mention="<@2>"))
        saved = {"rule": {"reads": [], "condition": "Has changed", "repeat": True},
                 "last_result": [{"value": 1}], "holding": True}
        with patch("elbow_helper.features.agent.scheduled.watchers._read_current",
                   AsyncMock(return_value=[{"value": 2}])), patch(
                   "elbow_helper.features.agent.scheduled.watchers._evaluate",
                   AsyncMock(return_value=(True, "Alert"))), patch(
                   "elbow_helper.features.agent.scheduled.watchers._send_alert",
                   AsyncMock()) as send:
            result = await check_watcher(context, saved)
        send.assert_not_awaited()
        self.assertEqual(result, ([{"value": 2}], True, False))

    async def test_scheduled_channel_reply_has_no_unseen_archive_request(self):
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=10)))
        message = ScheduledMessage(
            guild=SimpleNamespace(id=1), channel=channel,
            author=SimpleNamespace(id=2), content="Check status",
        )
        delivery = AgentDeliveryMixin()
        delivery.bot = SimpleNamespace(user=None)
        delivery._archive_reply = AsyncMock()
        await delivery._send_response(message, "Status checked", None)
        channel.send.assert_awaited_once()
        delivery._archive_reply.assert_not_awaited()


class SavedRequestScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_scope_action_also_delivers_output_steps(self):
        async def recheck():
            return True

        async def run():
            return None

        proposal = PreparedAction(
            "add_discord_roles", {"role_id": 12, "member_ids": [21]},
            ChangePreview(("Add role",), recheck), run,
        )
        state = AgentTurnState(command_outcomes=[
            CommandOutcome("complete", "public", text="Roster posted."),
        ])
        context = AgentRequestContext(
            bot=None, guild=None, member=SimpleNamespace(id=2),
            source_message=object(), account_links=None, clan_health=None,
            message_search=None, state=state,
        )
        service = SimpleNamespace(answer=AsyncMock(side_effect=lambda **_: (
            state.command_proposals.append(proposal) or "Preview")))
        runner = SimpleNamespace(submit=AsyncMock(return_value="run-1"),
                                 wait_run=AsyncMock(return_value={"status": "completed"}))
        agent = SimpleNamespace(service=service, action_runner=runner,
                                _send_response=AsyncMock())
        rule = {"request": "Do both", "allowed_actions": [{
            "capability": "add_discord_roles", "fixed_values": {"role_id": 12},
            "variable_fields": ["member_ids"], "max_targets": 2,
        }]}
        await AgentCog.run_saved_request(agent, context, rule)
        agent._send_response.assert_awaited_once()
        self.assertEqual(agent._send_response.await_args.args[1], "Roster posted.")
        self.assertEqual(agent._send_response.await_args.kwargs["context"].state.command_proposals,
                         [])

    async def test_confirmed_scope_runs_without_a_second_confirmation(self):
        async def recheck():
            return True

        async def run():
            return None

        proposal = PreparedAction(
            "add_discord_roles", {"role_id": 12, "member_ids": [21]},
            ChangePreview(("Add role",), recheck), run,
        )
        state = SimpleNamespace(command_proposals=[], command_outcomes=[], attachments=[])
        context = SimpleNamespace(source_message=object(), state=state,
                                  member=SimpleNamespace(id=2))
        service = SimpleNamespace(answer=AsyncMock(side_effect=lambda **_: (
            state.command_proposals.append(proposal) or "Preview")))
        action_runner = SimpleNamespace(submit=AsyncMock(return_value="run-1"),
                                        wait_run=AsyncMock(return_value={"status": "completed"}))
        agent = SimpleNamespace(service=service, action_runner=action_runner,
                                _send_response=AsyncMock())
        rule = {"request": "Give the role", "allowed_actions": [{
            "capability": "add_discord_roles", "fixed_values": {"role_id": 12},
            "variable_fields": ["member_ids"], "max_targets": 2,
        }]}
        await AgentCog.run_saved_request(agent, context, rule)
        action_runner.submit.assert_awaited_once()
        agent._send_response.assert_not_awaited()

    async def test_action_outside_confirmed_scope_gets_normal_preview(self):
        async def recheck():
            return True

        async def run():
            return None

        proposal = PreparedAction(
            "add_discord_roles", {"role_id": 13, "member_ids": [21]},
            ChangePreview(("Add role",), recheck), run,
        )
        state = SimpleNamespace(command_proposals=[], command_outcomes=[], attachments=[])
        context = SimpleNamespace(source_message=object(), state=state,
                                  member=SimpleNamespace(id=2))
        service = SimpleNamespace(answer=AsyncMock(side_effect=lambda **_: (
            state.command_proposals.append(proposal) or "Preview")))
        action_runner = SimpleNamespace(submit=AsyncMock())
        agent = SimpleNamespace(service=service, action_runner=action_runner,
                                _send_response=AsyncMock())
        rule = {"request": "Give the role", "allowed_actions": [{
            "capability": "add_discord_roles", "fixed_values": {"role_id": 12},
            "variable_fields": ["member_ids"], "max_targets": 2,
        }]}
        await AgentCog.run_saved_request(agent, context, rule)
        action_runner.submit.assert_not_awaited()
        agent._send_response.assert_awaited_once()
