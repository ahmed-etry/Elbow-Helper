"""Plan flow with synthetic capabilities and model responses."""

from __future__ import annotations

import json
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.models import AgentRequestContext, RegisteredAgentTool, AgentCapabilityEffect
from elbow_helper.features.agent.service import AgentService, AgentUnavailableError
from elbow_helper.features.agent.commands.bridge import build_command_tools
from elbow_helper.features.agent.commands.outcomes import CommandOutcome
from elbow_helper.features.agent.commands.confirmation import ChangePreview
from elbow_helper.features.agent.commands.registry import CommandAdapter
from elbow_helper.features.agent.wording import COMMAND_UNAVAILABLE
from elbow_helper.features.help.discovery import DiscoveredCommand, ParameterInfo
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.capabilities import CapabilityContract
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentToolDefinition, AgentUsage
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.infrastructure.ai import TextGenerationError


def _context():
    actor = SimpleNamespace(id=41, display_name="Test member",
                            roles=[SimpleNamespace(id=next(iter(CORE)))])
    channel = SimpleNamespace(
        id=91, type=discord.ChannelType.text, overwrites={},
        permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True),
    )
    role = SimpleNamespace(id=1)
    guild = SimpleNamespace(
        id=3, name="Test guild", me=actor, roles=[role, *actor.roles],
        default_role=role, get_member=lambda _: actor,
        get_channel_or_thread=lambda _: channel,
        channels=(channel,),
    )
    channel.guild = guild
    message = SimpleNamespace(id=71, channel=channel,
                              created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    return AgentRequestContext(
        bot=None, guild=guild, member=actor, source_message=message,
        account_links=None, clan_health=None, message_search=None,
    )


def _plan(steps, *, effort="low", periods=None):
    return {"goal": "Answer from the selected data", "effort": effort,
            "output": "text", "periods": periods or [], "entities": [],
            "steps": steps}


def _step(step_id, arguments=None, depends_on=None):
    return {"id": step_id, "capability": "read_value",
            "arguments": arguments or {}, "reason": "Read the selected value",
            "depends_on": depends_on or []}


def _model_step(plan):
    return AgentStep("", (AgentToolCall("plan-" + str(id(plan)),
                                   "submit_request_plan", json.dumps(plan)),), AgentUsage())


class _Session:
    context_window_tokens = 1_000_000

    def __init__(self, steps, events):
        self.steps = list(steps)
        self.events = events
        self.calls = []

    async def advance(self, results=(), *, allow_tools=True,
                      reasoning_effort=None, max_output_tokens=None):
        self.events.append("model")
        self.calls.append((tuple(results), allow_tools, reasoning_effort))
        return self.steps.pop(0)


class _Model:
    def __init__(self, session):
        self.session = session
        self.request = None

    def create_agent_session(self, **kwargs):
        self.request = kwargs
        return self.session


class PlanFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []

        async def read(_, arguments):
            self.events.append("read")
            return {"value": arguments.get("value", 7)}

        self.registry = {"read_value": RegisteredAgentTool(
            AgentToolDefinition("read_value", "Read a value.", {
                "type": "object", "properties": {"value": {"type": "integer"}},
                "required": [],
            }), read,
        )}

    async def _answer(self, session, context=None, conversation_history=""):
        model = _Model(session)
        with patch("elbow_helper.features.agent.service.build_agent_tools",
                   return_value=self.registry):
            answer = await AgentService(model).answer(
                question="Use the supplied values", local_context="",
                context=context or _context(), conversation_history=conversation_history,
            )
        return answer, model

    async def test_direct_reply_uses_one_low_effort_call_and_only_the_plan_tool(self):
        session = _Session([AgentStep("Ready.", (), AgentUsage())], self.events)
        answer, model = await self._answer(session)
        self.assertEqual(answer, "Ready.")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][2], AgentReasoningEffort.LOW)
        self.assertEqual([tool.name for tool in model.request["tools"]],
                         ["submit_request_plan"])

    async def test_one_step_plan_uses_two_model_calls(self):
        plan = _plan([_step("first")], effort="high")
        session = _Session([_model_step(plan), AgentStep("Seven.", (), AgentUsage())],
                           self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Seven.")
        self.assertEqual(self.events, ["model", "read", "model"])
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[1][2], AgentReasoningEffort.HIGH)
        result = json.loads(session.calls[1][0][0].content)["results"]["first"]
        self.assertEqual(result["value"], 7)
        self.assertEqual(result["flags"], {"status": "complete", "truncated": False})

    async def test_dependent_steps_run_before_the_answer_call(self):
        plan = _plan([_step("first"), _step("second", {
            "value": {"step": "first", "path": ["value"]},
        }, ["first"])])
        session = _Session([_model_step(plan), AgentStep("Seven.", (), AgentUsage())],
                           self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Seven.")
        self.assertEqual(self.events, ["model", "read", "read", "model"])
        self.assertEqual(len(session.calls), 2)

    async def test_scope_revisions_stop_after_two(self):
        self.registry["read_value"] = replace(self.registry["read_value"], definition=AgentToolDefinition(
            "read_value", "Read a value.", {"type": "object", "properties": {
                "period": {"type": "integer", "minimum": 0}}, "required": ["period"]}))
        plans = [_plan([_step("first", {"period": n})], periods=[{"kind": "key", "field": "period", "value": n}])
                 for n in range(4)]
        session = _Session([_model_step(plan) for plan in plans], self.events)
        with patch.dict("elbow_helper.features.agent.capabilities.CONTRACTS", {
            "read_value": CapabilityContract((), ("period",))
        }), self.assertRaisesRegex(AgentUnavailableError, "revision limit"):
            await self._answer(session)
        self.assertEqual(len(session.calls), 4)

    async def test_answer_only_round_refuses_extra_calls_once(self):
        plan = _plan([_step("first")])
        extra = AgentToolCall("extra", "submit_request_plan", json.dumps(plan))
        session = _Session([
            _model_step(plan), AgentStep("", (extra,), AgentUsage()),
            AgentStep("Seven.", (), AgentUsage()),
        ], self.events)
        with patch("elbow_helper.features.agent.service.MAX_MODEL_ROUNDS", 2):
            answer, _ = await self._answer(session)
        self.assertEqual(answer, "Seven.")
        self.assertEqual(len(session.calls), 3)
        self.assertFalse(session.calls[1][1])
        self.assertEqual(json.loads(session.calls[2][0][0].content)["flags"]["reason"],
                         "answer_only")

    async def test_independent_steps_run_together_before_the_answer(self):
        arrived = 0
        both_started = asyncio.Event()

        async def read(_, arguments):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)
            self.events.append("read")
            return {"value": arguments["value"]}

        self.registry["read_value"] = replace(self.registry["read_value"], handler=read)
        plan = _plan([_step("first", {"value": 1}), _step("second", {"value": 2})])
        session = _Session([_model_step(plan), AgentStep("Both.", (), AgentUsage())],
                           self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Both.")
        self.assertEqual(arrived, 2)
        self.assertEqual(self.events, ["model", "read", "read", "model"])

    async def test_identical_lookup_runs_once(self):
        plan = _plan([_step("first"), _step("second")])
        session = _Session([_model_step(plan), AgentStep("Done.", (), AgentUsage())],
                           self.events)
        await self._answer(session)
        self.assertEqual(self.events.count("read"), 1)
        results = json.loads(session.calls[1][0][0].content)["results"]
        self.assertEqual(sorted(result["flags"]["status"] for result in results.values()),
                         ["complete", "failed"])

    async def test_request_deadline_blocks_lookup_and_reserves_answer(self):
        plan = _plan([_step("first")])
        session = _Session([_model_step(plan), AgentStep("No read.", (), AgentUsage())],
                           self.events)
        context = replace(_context(), deadline_monotonic=time.monotonic() + 0.01)
        answer, _ = await self._answer(session, context)
        self.assertEqual(answer, "No read.")
        self.assertNotIn("read", self.events)
        result = json.loads(session.calls[1][0][0].content)["results"]["first"]
        self.assertEqual(result["flags"]["status"], "failed")

    async def test_access_loss_after_plan_stops_before_lookup(self):
        plan = _plan([_step("first")])
        context = _context()
        session = _Session([_model_step(plan)], self.events)
        advance = session.advance

        async def revoke(*args, **kwargs):
            result = await advance(*args, **kwargs)
            context.member.roles.clear()
            return result

        session.advance = revoke
        with self.assertRaises(AgentAccessLost):
            await self._answer(session, context)
        self.assertNotIn("read", self.events)
        self.assertEqual(len(session.calls), 1)

    async def test_disclosure_rejection_uses_one_plan_correction(self):
        async def read(_, arguments):
            self.events.append("read")
            return {"channel_id": arguments["channel_id"]}

        self.registry = {"read_value": RegisteredAgentTool(
            AgentToolDefinition("read_value", "Read a value.", {
                "type": "object", "properties": {
                    "channel_id": {"type": "integer", "minimum": 1},
                }, "required": ["channel_id"],
            }), read,
        )}
        contract = CapabilityContract(
            (("channel_id", "discord_channel"),), (),
            channel_fields=("channel_id",), source_scope="channel_messages",
            result_channel_fields=("channel_id",),
        )
        plan = _plan([_step("first", {"channel_id": 202})])
        plan["entities"] = [{"kind": "discord_channel", "value": 202}]
        session = _Session([_model_step(plan), _model_step(plan)], self.events)
        with (
            patch.dict("elbow_helper.features.agent.capabilities.CONTRACTS",
                       {"read_value": contract}),
            patch("elbow_helper.features.agent.service.can_disclose_provenance",
                  return_value=False),
        ):
            with self.assertRaisesRegex(AgentUnavailableError, "cannot be shared"):
                await self._answer(session)
        self.assertEqual(len(session.calls), 2)
        self.assertNotIn("read", self.events)

    async def test_exhausted_context_does_not_run_tools_or_send_another_provider_request(self):
        session = _Session([_model_step(_plan([_step("first")]))], self.events)
        session.context_window_tokens = 100
        with self.assertRaisesRegex(AgentUnavailableError, "context room"):
            await self._answer(session)
        self.assertEqual(self.events, [])

    async def test_interrupted_provider_round_is_logged_as_unknown_usage(self):
        session = _Session([], self.events)

        async def fail(*args, **kwargs):
            raise TextGenerationError("synthetic_failure")

        session.advance = fail
        with self.assertLogs("elbow_helper.features.agent.service", level="INFO") as logs:
            with self.assertRaises(AgentUnavailableError):
                await self._answer(session)
        self.assertTrue(any("attempted_rounds=1" in line and "unknown_token_rounds=1" in line
                            for line in logs.output))

    async def test_success_without_usage_is_not_recorded_as_known_free(self):
        session = _Session([AgentStep("Ready.", (), AgentUsage())], self.events)
        with self.assertLogs("elbow_helper.features.agent.service", level="INFO") as logs:
            await self._answer(session)
        self.assertTrue(any("unknown_token_rounds=1" in line and "unknown_cache_rounds=1" in line
                            for line in logs.output))

    async def test_round_and_tool_diagnostics_include_identity_and_duration(self):
        reply = AgentStep("Ready.", (), AgentUsage(100, 50, 80, 20),
                          "provider-id", "synthetic-model", 7)
        session = _Session([_model_step(_plan([_step("first")])), reply], self.events)
        with self.assertLogs("elbow_helper.features.agent.service", level="INFO") as logs:
            await self._answer(session)
        self.assertTrue(any("provider_request_id=provider-id" in line and "provider_duration_ms=7" in line
                            and "model=synthetic-model" in line for line in logs.output))
        self.assertTrue(any("Agent tool:" in line and "request=71" in line and "elapsed_ms=" in line
                            for line in logs.output))

    async def test_replayed_source_access_is_checked_before_first_model_round(self):
        context = _context()
        context.source_message.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=False, read_message_history=False,
        )
        session = _Session([AgentStep("Ready.", (), AgentUsage())], self.events)
        with self.assertRaises(AgentAccessLost):
            await self._answer(session, context)
        self.assertEqual(session.calls, [])

    async def test_required_role_revoked_during_lookup_stops_before_second_round(self):
        context = _context()

        async def read(local, _):
            local.member.roles.clear()
            self.events.append("read")
            return {"value": 7}

        self.registry["read_value"] = replace(self.registry["read_value"], handler=read)
        session = _Session([_model_step(_plan([_step("first")]))], self.events)
        with self.assertRaises(AgentAccessLost):
            await self._answer(session, context)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(context.state.evidence, [])

    async def test_tool_budget_forces_next_round_to_answer(self):
        session = _Session([_model_step(_plan([_step("first")])),
                            AgentStep("Ready.", (), AgentUsage())], self.events)
        with patch("elbow_helper.features.agent.service.MAX_TOOL_CALLS", 1):
            await self._answer(session)
        self.assertFalse(session.calls[1][1])
        self.assertEqual(self.events, ["model", "read", "model"])

    async def test_final_answer_recovery_does_not_loop(self):
        plan = _plan([_step("first")])
        session = _Session([_model_step(plan)] * 3, self.events)
        with patch("elbow_helper.features.agent.service.MAX_MODEL_ROUNDS", 2):
            with self.assertRaisesRegex(AgentUnavailableError, "did not answer"):
                await self._answer(session)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(self.events.count("read"), 1)

    async def test_history_is_supplied_and_lookup_evidence_is_retained(self):
        context = _context()
        session = _Session([_model_step(_plan([_step("first")])),
                            AgentStep("Ready.", (), AgentUsage())], self.events)
        _, model = await self._answer(session, context, conversation_history="synthetic earlier turn")
        self.assertIn("synthetic earlier turn", model.request["prompt"])
        self.assertEqual(len(context.state.evidence), 1)
        evidence = json.loads(context.state.evidence[0])
        self.assertEqual(evidence["tool"], "read_value")
        self.assertEqual(json.loads(evidence["result"])["value"], 7)
        self.assertEqual(evidence["result_status"], "complete")

    async def test_observed_later_round_pressure_stops_further_tool_requests(self):
        plan = _plan([_step("first")])
        step = _model_step(plan)
        step = replace(step, usage=AgentUsage(prompt_tokens=80_000, completion_tokens=1_000))
        session = _Session([step, AgentStep("Ready.", (), AgentUsage())], self.events)
        session.context_window_tokens = 100_000
        await self._answer(session)
        self.assertFalse(session.calls[1][1])

    async def test_nested_result_references_are_checked_again_before_execution(self):
        async def read(_, arguments):
            self.events.append("read")
            return {"value": "wrong-type"}

        self.registry["read_value"] = replace(self.registry["read_value"], handler=read)
        plan = _plan([_step("first"), _step("second", {
            "value": {"step": "first", "path": ["value"]},
        }, ["first"])])
        session = _Session([_model_step(plan), AgentStep("Ready.", (), AgentUsage())], self.events)
        await self._answer(session)
        self.assertEqual(self.events.count("read"), 1)
        self.assertEqual(json.loads(session.calls[1][0][0].content)["results"]["second"]["flags"]["status"],
                         "failed")

    async def test_research_can_continue_past_old_round_limit(self):
        plans = [_plan([_step(str(value), {"value": value})]) for value in range(1, 13)]
        session = _Session([*map(_model_step, plans), AgentStep("Ready.", (), AgentUsage())], self.events)
        await self._answer(session)
        self.assertEqual(self.events.count("read"), 12)
        self.assertEqual(len(session.calls), 13)

    async def test_future_unused_evidence_does_not_prevent_next_lookup(self):
        step = replace(_model_step(_plan([_step("first")])), usage=AgentUsage(350_000, 1000))
        session = _Session([step, AgentStep("Ready.", (), AgentUsage())], self.events)
        await self._answer(session)
        self.assertEqual(self.events.count("read"), 1)
        self.assertTrue(session.calls[1][1])

    async def test_requested_tool_result_is_returned_to_the_same_session(self):
        session = _Session([_model_step(_plan([_step("first", {"value": 7})])),
                            AgentStep("Ready.", (), AgentUsage())], self.events)
        _, model = await self._answer(session)
        self.assertIs(model.session, session)
        self.assertEqual(json.loads(session.calls[1][0][0].content)["results"]["first"]["value"], 7)

    async def test_new_source_lost_before_next_model_round_is_removed_from_pending_results(self):
        context = _context()
        context.state.source_channels.add(91)
        checks = 0
        async def disclosure(local):
            nonlocal checks
            checks += 1
            if checks == 4:
                raise AgentAccessLost("synthetic_revocation")
        async def read(local, _):
            local.state.source_channels.add(202)
            return {"value": "unsent_evidence"}
        self.registry["read_value"] = replace(self.registry["read_value"], handler=read)
        session = _Session([_model_step(_plan([_step("first")])), AgentStep("Ready.", (), AgentUsage())], self.events)
        with patch("elbow_helper.features.agent.service.require_disclosure_access", side_effect=disclosure):
            await self._answer(session, context)
        self.assertNotIn("unsent_evidence", str(session.calls[1]))
        self.assertEqual(context.state.source_channels, {91})
        self.assertEqual(context.state.evidence, [])

    async def test_access_loss_for_evidence_already_sent_to_model_still_stops(self):
        context = _context()
        first = _plan([_step("first", {"value": 1})])
        second = _plan([_step("second", {"value": 2})])
        session = _Session([_model_step(first), _model_step(second)], self.events)
        advance = session.advance
        async def revoke(*args, **kwargs):
            result = await advance(*args, **kwargs)
            if len(session.calls) == 2:
                context.member.roles.clear()
            return result
        session.advance = revoke
        with self.assertRaises(AgentAccessLost):
            await self._answer(session, context)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(self.events.count("read"), 1)

    async def test_context_pressure_requests_final_answer_without_lowering_output_limit(self):
        session = _Session([_model_step(_plan([_step("first")])), AgentStep("Ready.", (), AgentUsage())], self.events)
        calls = []
        advance = session.advance
        async def observe(*args, **kwargs):
            calls.append(kwargs)
            return await advance(*args, **kwargs)
        session.advance = observe
        with patch("elbow_helper.features.agent.service.ContextBudget.can_continue_tools", return_value=False):
            await self._answer(session)
        self.assertFalse(calls[1]["allow_tools"])
        self.assertEqual(calls[1]["max_output_tokens"], 16_000)
        self.assertEqual(self.events.count("read"), 0)

    async def test_final_tool_requests_are_declined_at_each_budget(self):
        for budget, value in (("MAX_TOOL_CALLS", 1), ("MAX_EVIDENCE_CHARACTERS", 1), ("MAX_MODEL_ROUNDS", 2)):
            with self.subTest(budget=budget):
                self.events.clear()
                first = _plan([_step("first")])
                second = _plan([_step("second", {"value": 8})])
                session = _Session([_model_step(first), _model_step(second), AgentStep("Ready.", (), AgentUsage())], self.events)
                with patch("elbow_helper.features.agent.service." + budget, value):
                    await self._answer(session)
                self.assertFalse(session.calls[1][1])
                self.assertFalse(session.calls[2][1])
                self.assertEqual(self.events.count("read"), 1)
                self.assertIn("answer_only", session.calls[2][0][0].content)

    async def test_dependent_period_is_bound_to_the_owner_result(self):
        contract = CapabilityContract((), ("selected_key",), period_results=(("key",),))
        async def read(_, arguments):
            self.events.append(arguments)
            return {"key": "synthetic-key", "value": 7}
        self.registry["read_value"] = replace(self.registry["read_value"],
            definition=AgentToolDefinition("read_value", "Read a value.", {
                "type": "object", "properties": {"selected_key": {"type": "string"}}}), handler=read)
        period = {"kind": "resolved", "step": "first", "selector": "latest", "path": ["key"]}
        plan = _plan([_step("first"), _step("second", {"selected_key": {"step": "first", "path": ["key"]}}, ["first"])], periods=[period])
        session = _Session([_model_step(plan), AgentStep("Ready.", (), AgentUsage())], self.events)
        with patch.dict("elbow_helper.features.agent.capabilities.CONTRACTS", {"read_value": contract}):
            await self._answer(session)
        self.assertEqual(self.events, ["model", {}, {"selected_key": "synthetic-key"}, "model"])

    async def test_lost_unpublished_batch_is_discarded_and_remaining_lookup_continues(self):
        context = _context()
        checks = 0
        async def disclosure(local):
            nonlocal checks
            checks += 1
            if checks == 3:
                raise AgentAccessLost("synthetic_revocation")
        async def read(local, arguments):
            self.events.append("read")
            if arguments.get("value") == 1:
                local.state.source_channels.add(202)
                return {"value": "discarded_evidence"}
            return {"value": "remaining_evidence"}
        self.registry["read_value"] = replace(self.registry["read_value"], handler=read)
        session = _Session([_model_step(_plan([_step("first", {"value": 1})])),
                            _model_step(_plan([_step("second", {"value": 2})])),
                            AgentStep("Ready.", (), AgentUsage())], self.events)
        with patch("elbow_helper.features.agent.service.require_disclosure_access", side_effect=disclosure):
            await self._answer(session, context)
        self.assertNotIn("discarded_evidence", str(session.calls[1]))
        self.assertIn("remaining_evidence", str(session.calls[2]))
        self.assertEqual(self.events.count("read"), 2)
        self.assertEqual(context.state.source_channels, set())
        self.assertEqual(len(context.state.evidence), 1)

    async def test_revoked_evidence_never_reaches_a_second_model_round(self):
        context = _context()
        session = _Session([_model_step(_plan([_step("first")]))], self.events)
        advance = session.advance
        async def revoke(*args, **kwargs):
            result = await advance(*args, **kwargs)
            context.source_message.channel.permissions_for = lambda _: SimpleNamespace(
                view_channel=False, read_message_history=False)
            return result
        session.advance = revoke
        with self.assertRaises(AgentAccessLost):
            await self._answer(session, context)
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("read", self.events)

    async def test_deepseek_dsml_after_lookup_limit_recovers_through_real_adapter(self):
        from elbow_helper.infrastructure.ai.client import DeepSeekTextClient
        first = _model_step(_plan([_step("first")])).tool_calls[0]
        dsml = '<||DSML|| calls><||DSML|| invoke name="submit_request_plan"></||DSML|| invoke></||DSML|| calls>'
        messages = [SimpleNamespace(content="", reasoning_content="retained reasoning", tool_calls=[
            SimpleNamespace(id=first.call_id, type="function", function=SimpleNamespace(
                name=first.name, arguments=first.arguments))]),
            SimpleNamespace(content=dsml, reasoning_content="retained reasoning", tool_calls=None),
            SimpleNamespace(content="Ready.", reasoning_content="retained reasoning", tool_calls=None)]
        responses = [SimpleNamespace(choices=[SimpleNamespace(message=message)],
                     usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
                     id=str(index), model="synthetic-model") for index, message in enumerate(messages)]
        create = AsyncMock(side_effect=responses)
        transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with (patch("elbow_helper.infrastructure.ai.client.AsyncOpenAI", return_value=transport),
              patch("elbow_helper.features.agent.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.features.agent.service.MAX_TOOL_CALLS", 1)):
            answer = await AgentService(DeepSeekTextClient("synthetic-key")).answer(
                question="synthetic request", local_context="", context=_context())
        self.assertEqual(answer, "Ready.")
        self.assertEqual(create.await_count, 3)
        self.assertEqual(self.events.count("read"), 1)
        final = create.await_args.kwargs
        self.assertEqual(final["tool_choice"], "none")
        self.assertEqual(next(message for message in reversed(final["messages"])
                              if message["role"] == "assistant")["reasoning_content"], "retained reasoning")
        self.assertIn("answer_only", next(message for message in reversed(final["messages"])
                                           if message["role"] == "tool")["content"])
        prompts = [call.kwargs["messages"][0]["content"] for call in create.await_args_list]
        self.assertEqual(len(set(prompts)), 1)

    async def test_reordering_and_narrowing_entities_do_not_use_revisions(self):
        plans = [_plan([_step(str(index), {"value": index + 1})]) for index in range(5)]
        values = ((101, 202), (202, 101), (101,), (202, 101), (202,))
        for plan, selected in zip(plans, values):
            plan["entities"] = [{"kind": "synthetic_source", "value": value} for value in selected]
        session = _Session([*map(_model_step, plans), AgentStep("Ready.", (), AgentUsage())], self.events)
        with patch("elbow_helper.features.agent.service.MAX_SCOPE_REVISIONS", 0):
            await self._answer(session)
        self.assertEqual(self.events.count("read"), 5)

    async def test_selected_artifact_keeps_a_tool_slot_after_research_budget_ends(self):
        artifact = AsyncMock(return_value={"artifact_prepared": True})
        self.registry["write_value"] = RegisteredAgentTool(AgentToolDefinition(
            "write_value", "Write a value.", {"type": "object", "properties": {"value": {"type": "integer"}}}),
            artifact, AgentCapabilityEffect.ARTIFACT)
        output = {**_step("output", {"value": {"step": "first", "path": ["value"]}}, ["first"]),
                  "capability": "write_value"}
        plan = _plan([_step("first"), output])
        plan["output"] = "write_value"
        session = _Session([_model_step(plan), AgentStep("Ready.", (), AgentUsage())], self.events)
        with patch("elbow_helper.features.agent.service.MAX_TOOL_CALLS", 2):
            await self._answer(session)
        artifact.assert_awaited_once()
        self.assertEqual(artifact.await_args.args[1], {"value": 7})
        self.assertFalse(session.calls[1][1])
        self.assertTrue(json.loads(session.calls[1][0][0].content)["results"]["output"]["artifact_prepared"])

    async def test_artifact_preparation_can_follow_completed_research(self):
        async def read(_, arguments):
            return {"error": "The source is unavailable."}
        self.registry["read_value"] = replace(self.registry["read_value"], handler=read)
        artifact = AsyncMock(return_value={"artifact_prepared": True})
        self.registry["write_value"] = RegisteredAgentTool(AgentToolDefinition(
            "write_value", "Write a value.", {"type": "object", "properties": {}}),
            artifact, AgentCapabilityEffect.ARTIFACT)
        output = {**_step("output", depends_on=["first"]), "capability": "write_value"}
        plan = _plan([_step("first"), output])
        plan["output"] = "write_value"
        session = _Session([_model_step(plan), AgentStep("Ready.", (), AgentUsage())], self.events)
        await self._answer(session)
        artifact.assert_awaited_once()
        data = json.loads(session.calls[1][0][0].content)["results"]
        self.assertEqual(data["first"]["flags"]["status"], "failed")
        self.assertTrue(data["output"]["artifact_prepared"])

    async def test_finite_context_can_keep_tools_available_for_first_round(self):
        session = _Session([AgentStep("Ready.", (), AgentUsage())], self.events)
        session.context_window_tokens = 150_000
        limits = []
        advance = session.advance
        async def observe(*args, **kwargs):
            limits.append(kwargs["max_output_tokens"])
            return await advance(*args, **kwargs)
        session.advance = observe
        _, model = await self._answer(session)
        self.assertTrue(session.calls[0][1])
        self.assertEqual(limits, [8_000])
        self.assertEqual(model.request["max_output_tokens"], 64_000)

    async def test_finite_context_executes_bounded_read_and_reserves_answer(self):
        session = _Session([_model_step(_plan([_step("first")])), AgentStep("Ready.", (), AgentUsage())], self.events)
        session.context_window_tokens = 150_000
        limits = []
        advance = session.advance
        async def observe(*args, **kwargs):
            limits.append(kwargs["max_output_tokens"])
            return await advance(*args, **kwargs)
        session.advance = observe
        await self._answer(session)
        self.assertEqual(self.events.count("read"), 1)
        self.assertEqual(limits, [8_000, 16_000])

    async def test_deadline_rechecked_after_model_round_before_tool_runs(self):
        now = time.monotonic()
        context = replace(_context(), deadline_monotonic=now + 200)
        session = _Session([_model_step(_plan([_step("first")])), AgentStep("Ready.", (), AgentUsage())], self.events)
        advance = session.advance
        clock = SimpleNamespace(value=now)
        with patch("elbow_helper.features.agent.service.time", SimpleNamespace(monotonic=lambda: clock.value)):
            async def advance_clock(*args, **kwargs):
                result = await advance(*args, **kwargs)
                clock.value = now + 150
                return result
            session.advance = advance_clock
            await self._answer(session, context)
        self.assertNotIn("read", self.events)
        self.assertEqual(len(session.calls), 2)

    async def test_retained_pages_keep_the_original_source_through_service(self):
        context = _context()
        context.state.source_channels.add(202)
        async def read(local, arguments):
            local.state.source_channels.add(arguments["channel_id"])
            return {"resource_id": "synthetic-id", "channel_id": arguments["channel_id"]}
        page = AsyncMock(return_value={"channel_id": 91, "value": 7})
        self.registry = {
            "read_value": RegisteredAgentTool(AgentToolDefinition("read_value", "Read a value.", {
                "type": "object", "properties": {"channel_id": {"type": "integer"}}, "required": ["channel_id"]}), read),
            "read_page": RegisteredAgentTool(AgentToolDefinition("read_page", "Read a page.", {
                "type": "object", "properties": {"resource_id": {"type": "string"}}, "required": ["resource_id"]}), page)}
        contracts = {
            "read_value": CapabilityContract((("channel_id", "discord_channel"),), (), channel_fields=("channel_id",),
                result_channel_fields=("channel_id",), source_scope="channel_messages", result_sources_within_query=True),
            "read_page": CapabilityContract((("resource_id", "synthetic_report"),), (), retained_fields=("resource_id",),
                result_channel_fields=("channel_id",), source_scope="retained_channel_evidence")}
        output = {**_step("page", {"resource_id": {"step": "first", "path": ["resource_id"]}}, ["first"]), "capability": "read_page"}
        plan = _plan([_step("first", {"channel_id": 91}), output])
        plan["entities"] = [{"kind": "discord_channel", "value": 91}]
        for source in (91, 202):
            page.return_value = {"channel_id": source, "value": 7}
            page.reset_mock()
            session = _Session([_model_step(plan), AgentStep("Ready.", (), AgentUsage())], self.events)
            context.state.evidence.clear()
            with (patch.dict("elbow_helper.features.agent.capabilities.CONTRACTS", contracts),
                  patch("elbow_helper.features.agent.service.named_sources", return_value={"discord_channel": frozenset({91})})):
                await self._answer(session, context)
            page.assert_awaited_once()
            result = json.loads(session.calls[1][0][0].content)["results"]["page"]
            self.assertEqual(result["flags"]["status"], "complete" if source == 91 else "failed")
            self.assertEqual(json.loads(context.state.evidence[-1])["capability_scope"]["bound_source_channels"], [91])
            if source == 202:
                self.assertNotIn('"value": 7', session.calls[1][0][0].content)

    async def test_disabled_commands_keep_the_read_only_catalogue(self):
        session = _Session([AgentStep("Ready.", (), AgentUsage())], self.events)
        with patch("elbow_helper.features.agent.service.build_command_tools") as commands:
            _, model = await self._answer(session)
        commands.assert_not_called()
        self.assertNotIn("run_command_", model.request["system_prompt"])

    async def test_enabled_command_asks_once_then_runs_on_reply(self):
        run = AsyncMock(return_value=CommandOutcome("complete", text="Synthetic result"))
        path = "/synthetic"
        command = DiscoveredCommand(
            path, "registered", (ParameterInfo(
                "value", "A required value.", True, "integer"),))
        help_entry = SimpleNamespace(path=path, summary="Get a synthetic result.", details="Uses a value.")
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands", return_value={path: command}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, capabilities = build_command_tools(object(), (CommandAdapter(path, "public", run),))
        command_name = next(iter(tools))
        async def answer(plan, history=""):
            session = _Session([_model_step(plan)], self.events)
            model = _Model(session)
            context = _context()
            with (patch("elbow_helper.features.agent.service.build_agent_tools", return_value=self.registry),
                  patch("elbow_helper.features.agent.service.build_command_tools",
                        return_value=(tools, capabilities))):
                context = replace(context, bot=SimpleNamespace(tree=object()))
                response = await AgentService(model, actions_enabled=True).answer(
                    question="synthetic request", local_context="", context=context,
                    conversation_history=history)
            return response, session, context, model
        incomplete = _plan([{**_step("command"), "capability": command_name}])
        question, session, context, model = await answer(incomplete)
        self.assertEqual(question, "I still need:\n- A required value.")
        self.assertEqual(len(session.calls), 1)
        self.assertIn(command_name, model.request["system_prompt"])
        self.assertIn("Use only listed capabilities", model.request["system_prompt"])
        run.assert_not_awaited()
        complete = _plan([{**_step("command", {"value": 7}), "capability": command_name}])
        response, session, context, _ = await answer(complete, history=question)
        self.assertEqual(response, "Synthetic result")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(context.state.command_outcomes[0].text, "Synthetic result")
        run.assert_awaited_once()

    async def test_private_command_data_never_reaches_public_reply_or_model(self):
        run = AsyncMock(return_value=CommandOutcome(
            "complete", "private", private_parts=("synthetic private data",)))
        path = "/synthetic"
        help_entry = SimpleNamespace(path=path, summary="Get a result.", details="Uses no options.")
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={path: DiscoveredCommand(path, "registered", ())}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, capabilities = build_command_tools(object(), (CommandAdapter(path, "private", run),))
        plan = _plan([{**_step("command"), "capability": next(iter(tools))}])
        session = _Session([_model_step(plan)], self.events)
        context = _context()
        with (patch("elbow_helper.features.agent.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.features.agent.service.build_command_tools",
                    return_value=(tools, capabilities))):
            context = replace(context, bot=SimpleNamespace(tree=object()))
            answer = await AgentService(_Model(session), actions_enabled=True).answer(
                question="synthetic request", local_context="", context=context)
        self.assertNotIn("synthetic private data", answer)
        self.assertNotIn("synthetic private data", str(context.state.evidence))
        self.assertEqual(context.state.command_outcomes[0].private_parts,
                         ("synthetic private data",))
        self.assertEqual(len(session.calls), 1)

    async def test_command_reference_cannot_escape_named_sources(self):
        run = AsyncMock(return_value=CommandOutcome("complete", text="Unexpected"))
        path = "/synthetic"
        command = DiscoveredCommand(path, "registered", (
            ParameterInfo("target", "Select a target.", True, "integer"),
        ))
        help_entry = SimpleNamespace(path=path, summary="Get a result.", details="Uses one target.")
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, capabilities = build_command_tools(object(), (
                CommandAdapter(path, "public", run,
                               entity_options=(("target", "synthetic_source"),)),
            ))
        command_step = {**_step("command", {"target": {"step": "first", "path": ["value"]}},
                                ["first"]), "capability": next(iter(tools))}
        plan = _plan([_step("first", {"value": 202}), command_step])
        plan["entities"] = [{"kind": "synthetic_source", "value": 101}]
        session = _Session([_model_step(plan), AgentStep("Refused.", (), AgentUsage())], self.events)
        with (patch("elbow_helper.features.agent.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.features.agent.service.build_command_tools",
                    return_value=(tools, capabilities)),
              patch("elbow_helper.features.agent.service.named_sources",
                    return_value={"synthetic_source": frozenset({101})})):
            context = replace(_context(), bot=SimpleNamespace(tree=object()))
            await AgentService(_Model(session), actions_enabled=True).answer(
                question="synthetic request", local_context="", context=context)
        run.assert_not_awaited()
        data = json.loads(session.calls[1][0][0].content)["results"]
        self.assertEqual(data["command"]["flags"]["status"], "failed")

    async def test_confirmed_steps_make_one_preview_without_running(self):
        path = "/synthetic"
        run = AsyncMock(return_value=CommandOutcome("complete", text="Unexpected"))
        async def prepare(context, values):
            return ChangePreview((f"Change target {values['target']}",),
                                 AsyncMock(return_value=True))
        command = DiscoveredCommand(path, "registered", (
            ParameterInfo("target", "Select a target.", True, "integer"),
        ))
        help_entry = SimpleNamespace(path=path, summary="Change a value.", details="Uses one target.")
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, capabilities = build_command_tools(object(), (
                CommandAdapter(path, "confirm", run, prepare=prepare),
            ))
        name = next(iter(tools))
        plan = _plan([{**_step(str(value), {"target": value}), "capability": name}
                      for value in (101, 202)])
        session = _Session([_model_step(plan)], self.events)
        context = _context()
        with (patch("elbow_helper.features.agent.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.features.agent.service.build_command_tools",
                    return_value=(tools, capabilities))):
            context = replace(context, bot=SimpleNamespace(tree=object()))
            answer = await AgentService(_Model(session), actions_enabled=True).answer(
                question="synthetic request", local_context="", context=context)
        self.assertIn("Change target 101", answer)
        self.assertIn("Change target 202", answer)
        self.assertEqual(len(context.state.command_proposals), 2)
        self.assertEqual(len(session.calls), 1)
        run.assert_not_awaited()

    async def test_incomplete_change_preview_cannot_be_confirmed(self):
        path = "/synthetic"
        run = AsyncMock(return_value=CommandOutcome("complete", text="Unexpected"))
        async def prepare(context, values):
            if values["target"] == 202:
                raise ValueError("Synthetic target unavailable")
            return ChangePreview((f"Change target {values['target']}",),
                                 AsyncMock(return_value=True))
        command = DiscoveredCommand(path, "registered", (
            ParameterInfo("target", "Select a target.", True, "integer"),
        ))
        help_entry = SimpleNamespace(path=path, summary="Change a value.", details="Uses one target.")
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, capabilities = build_command_tools(object(), (
                CommandAdapter(path, "confirm", run, prepare=prepare),
            ))
        name = next(iter(tools))
        plan = _plan([{**_step(str(value), {"target": value}), "capability": name}
                      for value in (101, 202)])
        session = _Session([_model_step(plan)], self.events)
        context = _context()
        with (patch("elbow_helper.features.agent.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.features.agent.service.build_command_tools",
                    return_value=(tools, capabilities))):
            context = replace(context, bot=SimpleNamespace(tree=object()))
            answer = await AgentService(_Model(session), actions_enabled=True).answer(
                question="synthetic request", local_context="", context=context)
        self.assertEqual(answer, COMMAND_UNAVAILABLE)
        self.assertEqual(context.state.command_proposals, [])
        run.assert_not_awaited()
