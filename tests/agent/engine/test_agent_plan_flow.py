"""Plan flow with synthetic capabilities and model responses."""

from __future__ import annotations

from tests.agent.engine.helpers import patch_engine

import json
from contextlib import nullcontext
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import time
from elbow_helper.agent.conversation.wording import AGENT_ANSWER_UNFINISHED
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.roles import CORE
from elbow_helper.agent.engine.models import AgentRequestContext, RegisteredAgentTool, AgentCapabilityEffect
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.commands.bridge import build_command_tools
from elbow_helper.agent.actions.outcomes import ActionOutcome
from elbow_helper.agent.actions.contracts import ActionRefused, ChangePreview
from elbow_helper.agent.actions.contracts import ActionClass, PreparedAction
from elbow_helper.agent.commands.registry import CommandAdapter
from elbow_helper.agent.engine.prompts import LIMIT_ANSWER_INSTRUCTION
from elbow_helper.features.help.discovery import DiscoveredCommand, ParameterInfo
from elbow_helper.agent.safety.access import AgentAccessLost
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.engine.budgets import ContextBudget
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentToolDefinition, AgentUsage
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
        id=3, name="Test guild", me=SimpleNamespace(id=999, bot=True, roles=[]), chunked=True, members=[actor], roles=[role, *actor.roles],
        default_role=role, get_member=lambda _: actor,
        get_channel_or_thread=lambda _: channel,
        channels=(channel,),
    )
    channel.guild = guild
    message = SimpleNamespace(id=71, channel=channel,
                              created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    return AgentRequestContext(
        bot=None, guild=guild, member=actor, source_message=message,
        account_links=None, message_search=None,
    )


def _plan(steps, *, effort="low", periods=None):
    return {"goal": "Answer from the selected data", "effort": effort,
            "output": "text",
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
        self.instructions = []
        self.output_limits = []

    async def advance(self, results=(), *, allow_tools=True,
                      reasoning_effort=None, max_output_tokens=None,
                      continuation_instruction=None):
        self.events.append("model")
        self.calls.append((tuple(results), allow_tools, reasoning_effort))
        self.instructions.append(continuation_instruction)
        self.output_limits.append(max_output_tokens)
        return self.steps.pop(0)


class _Model:
    def __init__(self, session, *, route="research"):
        self.session = session
        self.request = None
        self.route = route
        self.routing_session = None
        self.routing_request = None

    def create_agent_session(self, **kwargs):
        from elbow_helper.agent.engine.routing import ROUTE_INSTRUCTION
        from tests.agent.engine.helpers import RoutingSession
        if kwargs["system_prompt"] == ROUTE_INSTRUCTION:
            self.routing_request = kwargs
            self.routing_session = RoutingSession(self.route)
            return self.routing_session if self.session is not None else None
        if self.request is None:
            self.request = kwargs
        return self.session


class PlanFlowTests(unittest.IsolatedAsyncioTestCase):


    async def test_invalid_step_keeps_valid_results_and_can_be_corrected(self):
        plan = _plan([
            _step("valid", {"value": 13}),
            _step("invalid", {"value": "wrong"}),
            _step("dependent", {
                "value": {"step": "invalid", "path": ["value"]},
            }, ["invalid"]),
            _step("indirect", {}, ["dependent"]),
        ])
        corrected = _plan([_step("invalid", {"value": 17})])
        session = _Session([
            _model_step(plan), _model_step(corrected),
            AgentStep("Synthetic answer", (), AgentUsage()),
        ], self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Synthetic answer")
        self.assertEqual(self.events, ["model", "read", "model", "read", "model"])
        results = json.loads(session.calls[1][0][0].content)["results"]
        self.assertEqual(results["valid"]["value"], 13)
        self.assertIn("value must match", results["invalid"]["error"])
        self.assertEqual(results["dependent"]["error"],
                         "Step invalid failed, so this step could not run.")
        self.assertEqual(results["indirect"]["error"],
                         "Step dependent failed, so this step could not run.")
        corrected = json.loads(session.calls[2][0][0].content)["results"]
        self.assertEqual(corrected["invalid"]["value"], 17)

    async def test_revision_replaces_failed_steps_before_their_dependents_run(self):
        dependent = _step("dependent", {
            "value": {"step": "first", "path": ["value"]},
        }, ["first"])
        first = _plan([_step("first", {"value": "invalid"}), dependent])
        revised = _plan([_step("first", {"value": 17}), dependent])
        session = _Session([
            _model_step(first), _model_step(revised),
            AgentStep("Synthetic answer", (), AgentUsage()),
        ], self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Synthetic answer")
        failed = json.loads(session.calls[1][0][0].content)["results"]
        self.assertIn("error", failed["first"])
        self.assertIn("error", failed["dependent"])
        replaced = json.loads(session.calls[2][0][0].content)["results"]
        self.assertEqual(replaced["first"]["value"], 17)
        self.assertEqual(replaced["dependent"]["value"], 17)
        self.assertEqual(len(session.calls), 3)

    async def test_result_references_supply_dependencies_in_plans_and_revisions(self):
        for revision in (False, True):
            with self.subTest(revision=revision):
                self.events.clear()
                source = _step("source")
                consumer = _step("consumer", {
                    "value": {"step": "source", "path": ["value"]},
                })
                plans = ([_plan([source]), _plan([consumer])] if revision
                         else [_plan([source, consumer])])
                session = _Session([
                    *(_model_step(plan) for plan in plans),
                    AgentStep("Synthetic answer", (), AgentUsage()),
                ], self.events)
                answer, _ = await self._answer(session)
                self.assertEqual(answer, "Synthetic answer")
                self.assertEqual(len(session.calls), 3 if revision else 2)
                results = json.loads(session.calls[-1][0][0].content)["results"]
                self.assertEqual(results["consumer"]["value"], 7)
                self.assertEqual(self.events.count("read"), 2)

    async def test_implicit_change_target_dependencies_are_shown_only_in_the_preview(self):
        async def prepare(context, arguments):
            self.assertEqual(arguments, {"value": 7})
            context.state.proposed_changes.append(PreparedAction(
                "synthetic_change", dict(arguments),
                ChangePreview(("Change a synthetic value",), AsyncMock(return_value=True)),
                AsyncMock(),
            ))
            return {"status": "confirmation_required"}

        self.registry["synthetic_change"] = RegisteredAgentTool(AgentToolDefinition(
            "synthetic_change", "Change a synthetic value.",
            {"type": "object", "properties": {"value": {"type": "integer"}}},
        ), prepare, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(()))
        plan = _plan([_step("source"), {
            **_step("change", {"value": {"step": "source", "path": ["value"]}}),
            "capability": "synthetic_change",
        }])
        session = _Session([_model_step(plan)], self.events)
        context = replace(_context(), bot=SimpleNamespace(tree=object()))
        with patch("elbow_helper.agent.engine.service.build_command_tools",
                   return_value=({}, {})):
            answer, _ = await self._answer(session, context)
        self.assertIn("Change a synthetic value", answer)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(context.state.proposed_changes), 1)


    async def test_model_truncation_preserves_structured_references_and_evidence(self):
        rows = [{"target_id": 101 + index, "details": "synthetic " * 100}
                for index in range(10)]

        async def read(context, arguments):
            return {"players": rows} if not arguments else {"value": arguments["value"]}

        self.registry["read_value"] = replace(
            self.registry["read_value"], handler=read,
            contract=CapabilityContract(()),
        )
        plan = _plan([_step("first"), _step("second", {
            "value": {"step": "first", "path": ["players", 0, "target_id"]},
        }, ["first"])])
        session = _Session([_model_step(plan), AgentStep("Synthetic answer", (), AgentUsage())], self.events)
        context = _context()
        with patch.object(ContextBudget, "result_character_limit", return_value=160):
            await self._answer(session, context)
        results = json.loads(session.calls[-1][0][0].content)["results"]
        self.assertTrue(results["first"]["flags"]["truncated"])
        self.assertEqual(results["second"]["value"], 101)
        record = json.loads(context.state.evidence[0])
        self.assertEqual(json.loads(record["result"])["players"], rows)
        self.assertFalse(record["result_complete"])


    async def test_prepared_actions_must_match_the_registered_class(self):
        for registered in ActionClass:
            for prepared in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE):
                with self.subTest(registered=registered, prepared=prepared):
                    async def prepare(context, arguments):
                        context.state.proposed_changes.append(PreparedAction(
                            "synthetic_change", {}, ChangePreview(
                                ("Synthetic preview",), AsyncMock(return_value=True)),
                            AsyncMock(), action_class=prepared,
                        ))
                        return {"status": "confirmation_required"}
                    tool = RegisteredAgentTool(AgentToolDefinition("synthetic_change", "Synthetic", {
                        "type": "object", "properties": {}, "required": [],
                    }), prepare, AgentCapabilityEffect.COMMAND, registered)
                    plan = _plan([{**_step("changed"), "capability": "synthetic_change"}])
                    session = _Session([_model_step(plan), AgentStep("Synthetic refusal", (), AgentUsage())],
                                       self.events)
                    context = replace(_context(), bot=SimpleNamespace(tree=object()))
                    mismatch = registered is not prepared
                    logs = (self.assertLogs("elbow_helper.agent.engine.steps", level="ERROR")
                            if mismatch else nullcontext())
                    with (patch("elbow_helper.agent.engine.service.build_agent_tools",
                                return_value={"synthetic_change": tool}),
                          patch("elbow_helper.agent.engine.service.build_command_tools",
                                return_value=({}, {})), logs):
                        answer = await AgentService(_Model(session)).answer(
                            question="Synthetic", local_context="", context=context,
                        )
                    self.assertEqual(bool(context.state.proposed_changes), not mismatch)
                    if mismatch:
                        self.assertEqual(answer, "Synthetic refusal")
                        if registered in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE):
                            self.assertTrue(session.calls[-1][1])
                        result = json.loads(session.calls[-1][0][0].content)["results"]["changed"]
                        self.assertEqual(result["flags"]["status"], "failed")
                    else:
                        self.assertIn("Synthetic preview", answer)


    async def test_failed_final_answer_retains_partial_content_without_a_canned_failure(self):
        session = _Session([_model_step(_plan([_step("first")])),
                            AgentStep("Part one", (), AgentUsage(), output_limit_reached=True)],
                           self.events)
        advance = session.advance
        async def fail_final(*args, **kwargs):
            if kwargs.get("continuation_instruction") == (
                LIMIT_ANSWER_INSTRUCTION
            ):
                raise TextGenerationError("synthetic_failure")
            return await advance(*args, **kwargs)
        session.advance = fail_final
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Part one")
        self.assertEqual(self.events.count("read"), 1)

    async def asyncSetUp(self):
        self.events = []

        async def read(_, arguments):
            self.events.append("read")
            return {"value": arguments.get("value", 7)}

        self.registry = {"read_value": RegisteredAgentTool(
            AgentToolDefinition("read_value", "Read a value.", {
                "type": "object", "properties": {"value": {"type": "integer"}},
                "required": [],
            }), read, contract=CapabilityContract(()),
        )}

    async def _answer(self, session, context=None, conversation_history=""):
        model = _Model(session)
        with patch("elbow_helper.agent.engine.service.build_agent_tools",
                   return_value=self.registry):
            answer = await AgentService(model).answer(
                question="Use the supplied values", local_context="",
                context=context or _context(), conversation_history=conversation_history,
            )
        return answer, model


    async def test_cut_off_direct_reply_continues_once(self):
        session = _Session([
            AgentStep("First part", (), AgentUsage(), output_limit_reached=True),
            AgentStep("last part.", (), AgentUsage()),
        ], self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "First part\nlast part.")
        self.assertEqual(len(session.calls), 2)

    async def test_cut_off_plan_is_reissued_before_parsing(self):
        complete = _plan([_step("first")])
        session = _Session([
            AgentStep("", (AgentToolCall("partial", "submit_request_plan", "{"),),
                      AgentUsage(), output_limit_reached=True),
            _model_step(complete),
            AgentStep("Seven.", (), AgentUsage()),
        ], self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Seven.")
        self.assertIn("incomplete", session.calls[1][0][0].content)
        self.assertEqual(self.events, ["model", "model", "read", "model"])

    async def test_second_output_limit_retains_both_parts_without_another_call(self):
        session = _Session([
            AgentStep("First part", (), AgentUsage(), output_limit_reached=True),
            AgentStep("Second part", (), AgentUsage(), output_limit_reached=True),
        ], self.events)
        answer, _ = await self._answer(session)
        self.assertIn("First part", answer)
        self.assertIn("Second part", answer)
        self.assertIn(AGENT_ANSWER_UNFINISHED, answer)
        self.assertEqual(len(session.calls), 2)

    async def test_wrong_result_path_can_be_corrected_in_a_later_plan(self):
        bad = _plan([_step("first"), _step("second", {
            "value": {"step": "first", "path": ["id"]},
        }, ["first"])])
        good = _plan([_step("third", {
            "value": {"step": "first", "path": ["value"]},
        }, ["first"])])
        session = _Session([_model_step(bad), _model_step(good),
                            AgentStep("Seven.", (), AgentUsage())], self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Seven.")
        self.assertEqual(self.events, ["model", "read", "model", "read", "model"])
        self.assertIn("first", session.calls[1][0][0].content)
        self.assertIn("required earlier result", session.calls[1][0][0].content)


    async def test_initial_plan_and_each_revision_get_one_correction(self):
        initial = _plan([_step("initial", {"value": 1})])
        invalid_initial = {**initial, "output": "unavailable"}
        first_revision = _plan([_step("revision_one", {"value": 2})])
        session = _Session([
            _model_step(invalid_initial), _model_step(initial),
            _model_step({**first_revision, "output": "unavailable"}),
            _model_step(first_revision),
            AgentStep("Synthetic request complete.", (), AgentUsage()),
        ], self.events)
        session.steps = [replace(step, usage=AgentUsage(4000, 100)) for step in session.steps]
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Synthetic request complete.")
        self.assertEqual(self.events.count("read"), 2)
        self.assertEqual(len(session.calls), 5)
        for index in (1, 3):
            self.assertIn("rule", session.calls[index][0][0].content)
        self.assertEqual(json.loads(session.calls[-1][0][0].content)["results"]["revision_one"]["value"], 2)

    async def test_invalid_revision_correction_does_not_get_another_attempt(self):
        initial = _plan([_step("initial")])
        invalid = {**_plan([_step("revision")]), "output": "unavailable"}
        for broken in (_model_step(invalid), AgentStep("", (
                AgentToolCall("synthetic-broken", "submit_request_plan", "{"),), AgentUsage())):
            with self.subTest(arguments=broken.tool_calls[0].arguments):
                self.events.clear()
                session = _Session([_model_step(initial), broken, broken,
                                    _model_step(_plan([_step("unexpected")]))], self.events)
                answer, _ = await self._answer(session)
                self.assertIn(AGENT_ANSWER_UNFINISHED, answer)
                self.assertEqual(self.events.count("read"), 1)
                self.assertEqual(len(session.calls), 4)
                self.assertFalse(session.calls[-1][1])


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
                         ["complete", "complete"])

    async def test_live_reads_recheck_freshness_in_revised_plans(self):
        original = self.registry.pop("read_value")
        self.registry["read_clash"] = replace(
            original, definition=replace(original.definition, name="read_clash"),
        )
        plans = []
        for step_id in ("first", "second"):
            step = _step(step_id)
            step["capability"] = "read_clash"
            plans.append(_model_step(_plan([step])))
        session = _Session([*plans, AgentStep("Done.", (), AgentUsage())], self.events)
        await self._answer(session)
        self.assertEqual(self.events.count("read"), 2)


    async def test_revision_keeps_successful_step_ids_reserved(self):
        for payload in ({"value": 7}, {}, {"value": 7, "flags": {"status": "partial"}}):
            with self.subTest(payload=payload):
                self.events.clear()

                async def read(context, arguments):
                    self.events.append("read")
                    return payload

                self.registry["read_value"] = replace(self.registry["read_value"], handler=read)
                first = _plan([_step("first", {"value": 7})])
                session = _Session([_model_step(first), _model_step(first),
                                    AgentStep("Done.", (), AgentUsage())], self.events)
                await self._answer(session)
                self.assertIn("Use a new step ID.", session.calls[-1][0][0].content)
                self.assertEqual(self.events.count("read"), 1)

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


    async def test_exhausted_context_does_not_run_tools_or_send_another_provider_request(self):
        session = _Session([_model_step(_plan([_step("first")]))], self.events)
        session.context_window_tokens = 100
        answer, _ = await self._answer(session)
        self.assertIn(AGENT_ANSWER_UNFINISHED, answer)
        self.assertEqual(self.events, [])


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


    async def test_nested_result_references_are_checked_again_before_execution(self):
        async def read(_, arguments):
            self.events.append("read")
            return {"value": "wrong-type"}

        self.registry["read_value"] = replace(self.registry["read_value"], handler=read)
        plan = _plan([_step("first"), _step("second", {
            "value": {"step": "first", "path": ["value"]},
        }, ["first"])])
        session = _Session([_model_step(plan), AgentStep("Ready.", (), AgentUsage())], self.events)
        with self.assertLogs("elbow_helper.agent.engine.steps", level="WARNING") as logs:
            await self._answer(session)
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertEqual(logs.records[0].getMessage(),
                         "Agent planned step refused: step=second capability=read_value "
                         'error=Invalid arguments: value must match {"type":"integer"}')
        self.assertNotIn("wrong-type", " ".join(logs.output))
        self.assertEqual(self.events.count("read"), 1)
        self.assertEqual(json.loads(session.calls[1][0][0].content)["results"]["second"]["flags"]["status"],
                         "failed")


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
        with patch_engine("require_evidence_access", side_effect=disclosure):
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


    async def test_lost_unpublished_batch_is_discarded_and_remaining_lookup_continues(self):
        context = _context()
        checks = 0
        async def disclosure(local):
            nonlocal checks
            checks += 1
            if 202 in local.state.source_channels:
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
        with patch_engine("require_evidence_access", side_effect=disclosure):
            await self._answer(session, context)
        self.assertNotIn("discarded_evidence", str(session.calls[1]))
        self.assertIn("remaining_evidence", str(session.calls[2]))
        self.assertEqual(self.events.count("read"), 2)
        self.assertEqual(context.state.source_channels, set())
        self.assertEqual(len(context.state.evidence), 1)


    async def test_deepseek_dsml_during_compact_synthesis_recovers_through_real_adapter(self):
        from elbow_helper.infrastructure.ai.client import DeepSeekTextClient
        first = _model_step(_plan([_step("first")])).tool_calls[0]
        dsml = '<||DSML|| calls><||DSML|| invoke name="submit_request_plan"></||DSML|| invoke></||DSML|| calls>'
        messages = [SimpleNamespace(content="research", reasoning_content="", tool_calls=None),
            SimpleNamespace(content="", reasoning_content="retained reasoning", tool_calls=[
            SimpleNamespace(id=first.call_id, type="function", function=SimpleNamespace(
                name=first.name, arguments=first.arguments))]),
            SimpleNamespace(content=dsml, reasoning_content="retained reasoning", tool_calls=None),
            SimpleNamespace(content="Ready.", reasoning_content="retained reasoning", tool_calls=None)]
        responses = [SimpleNamespace(choices=[SimpleNamespace(message=message)],
                     usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
                     id=str(index), model="synthetic-model") for index, message in enumerate(messages)]
        create = AsyncMock(side_effect=responses)
        transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        transport.with_options = lambda **kwargs: transport
        with (patch("elbow_helper.infrastructure.ai.client.AsyncOpenAI", return_value=transport),
              patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.agent.engine.budgets.MAX_TOOL_CALLS", 1)):
            answer = await AgentService(DeepSeekTextClient("synthetic-key")).answer(
                question="synthetic request", local_context="", context=_context())
        self.assertEqual(answer, "Ready.")
        self.assertEqual(create.await_count, 4)
        self.assertEqual(self.events.count("read"), 1)
        final = create.await_args.kwargs
        self.assertEqual(final["tool_choice"], "none")
        self.assertNotIn("tools", final)
        self.assertFalse(any(message["role"] == "tool" for message in final["messages"]))
        self.assertIn("Available evidence:", final["messages"][1]["content"])
        prompts = [call.kwargs["messages"][0]["content"] for call in create.await_args_list[1:]]
        self.assertEqual(prompts[1], prompts[2])
        self.assertNotIn("<capabilities>", prompts[1])
        self.assertIn("<capabilities>", prompts[0])


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
        with patch("elbow_helper.agent.engine.budgets.MAX_TOOL_CALLS", 2):
            await self._answer(session)
        artifact.assert_awaited_once()
        self.assertEqual(artifact.await_args.args[1], {"value": 7})
        self.assertFalse(session.calls[1][1])
        self.assertTrue(json.loads(session.calls[1][0][0].content)["results"]["output"]["artifact_prepared"])

    async def test_artifact_preparation_cannot_depend_on_failed_research(self):
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
        artifact.assert_not_awaited()
        data = json.loads(session.calls[1][0][0].content)["results"]
        self.assertEqual(data["first"]["flags"]["status"], "failed")
        self.assertEqual(data["output"]["error"],
                         "Step first failed, so this step could not run.")


    async def test_deadline_rechecked_after_model_round_before_tool_runs(self):
        now = time.monotonic()
        context = replace(_context(), deadline_monotonic=now + 200)
        session = _Session([_model_step(_plan([_step("first")])), AgentStep("Ready.", (), AgentUsage())], self.events)
        advance = session.advance
        clock = SimpleNamespace(value=now)
        with patch_engine("time", SimpleNamespace(monotonic=lambda: clock.value)):
            async def advance_clock(*args, **kwargs):
                result = await advance(*args, **kwargs)
                clock.value = now + 150
                return result
            session.advance = advance_clock
            await self._answer(session, context)
        self.assertNotIn("read", self.events)
        self.assertEqual(len(session.calls), 2)


    async def test_enabled_command_asks_once_then_runs_on_reply(self):
        run = AsyncMock(return_value=ActionOutcome("complete", text="Synthetic result"))
        path = "/synthetic"
        command = DiscoveredCommand(
            path, "registered", (ParameterInfo(
                "value", "A required value.", True, "integer"),))
        help_entry = SimpleNamespace(path=path, summary="Get a synthetic result.", details="Uses a value.")
        with (patch(
            "elbow_helper.agent.commands.registry.discover_commands",
            return_value={path: command},
        ),
              patch("elbow_helper.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, capabilities = build_command_tools(object(), (CommandAdapter(path, "public", run),))
        command_name = next(iter(tools))
        async def answer(plan, history=""):
            session = _Session([
                _model_step(plan), AgentStep("Which value should I use?", (), AgentUsage()),
            ], self.events)
            model = _Model(session)
            context = _context()
            with (patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=self.registry),
                  patch("elbow_helper.agent.engine.service.build_command_tools",
                        return_value=(tools, capabilities))):
                context = replace(context, bot=SimpleNamespace(tree=object()))
                response = await AgentService(model).answer(
                    question="synthetic request", local_context="", context=context,
                    conversation_history=history)
            return response, session, context, model
        incomplete = _plan([{**_step("command"), "capability": command_name}])
        question, session, context, model = await answer(incomplete)
        self.assertEqual(question, "Which value should I use?")
        self.assertEqual(len(session.calls), 2)
        missing_data = json.loads(session.calls[1][0][0].content)
        self.assertEqual(missing_data["missing_options"], [{
            "name": "value", "description": "A required value.", "choices": [],
        }])
        self.assertIn(command_name, model.request["system_prompt"])
        self.assertIn("Do the task with your capabilities", model.request["system_prompt"])
        run.assert_not_awaited()
        complete = _plan([{**_step("command", {"value": 7}), "capability": command_name}])
        response, session, context, _ = await answer(complete, history=question)
        self.assertEqual(response, "Synthetic result")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(context.state.outcomes[0].text, "Synthetic result")
        run.assert_awaited_once()

    async def test_private_command_data_never_reaches_public_reply_or_model(self):
        run = AsyncMock(return_value=ActionOutcome(
            "complete", "private", private_parts=("synthetic private data",)))
        path = "/synthetic"
        help_entry = SimpleNamespace(path=path, summary="Get a result.", details="Uses no options.")
        with (patch("elbow_helper.agent.commands.registry.discover_commands",
                    return_value={path: DiscoveredCommand(path, "registered", ())}),
              patch("elbow_helper.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, capabilities = build_command_tools(object(), (CommandAdapter(path, "private", run),))
        plan = _plan([{**_step("command"), "capability": next(iter(tools))}])
        session = _Session([_model_step(plan)], self.events)
        context = _context()
        with (patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.agent.engine.service.build_command_tools",
                    return_value=(tools, capabilities))):
            context = replace(context, bot=SimpleNamespace(tree=object()))
            answer = await AgentService(_Model(session)).answer(
                question="synthetic request", local_context="", context=context)
        self.assertNotIn("synthetic private data", answer)
        self.assertNotIn("synthetic private data", str(context.state.evidence))
        self.assertEqual(context.state.outcomes[0].private_parts,
                         ("synthetic private data",))
        self.assertEqual(len(session.calls), 1)


    async def test_confirmed_steps_make_one_preview_without_running(self):
        path = "/synthetic"
        run = AsyncMock(return_value=ActionOutcome("complete", text="Unexpected"))
        async def prepare(context, values):
            return ChangePreview((f"Change target {values['target']}",),
                                 AsyncMock(return_value=True))
        command = DiscoveredCommand(path, "registered", (
            ParameterInfo("target", "Select a target.", True, "integer"),
        ))
        help_entry = SimpleNamespace(path=path, summary="Change a value.", details="Uses one target.")
        with (patch("elbow_helper.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, capabilities = build_command_tools(object(), (
                CommandAdapter(path, "confirm", run, prepare=prepare),
            ))
        name = next(iter(tools))
        plan = _plan([{**_step(str(value), {"target": value}), "capability": name}
                      for value in (101, 202)])
        session = _Session([_model_step(plan)], self.events)
        context = _context()
        with (patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.agent.engine.service.build_command_tools",
                    return_value=(tools, capabilities))):
            context = replace(context, bot=SimpleNamespace(tree=object()))
            answer = await AgentService(_Model(session)).answer(
                question="synthetic request", local_context="", context=context)
        self.assertIn("Change target 101", answer)
        self.assertIn("Change target 202", answer)
        self.assertEqual(len(context.state.proposed_changes), 2)
        self.assertEqual(len(session.calls), 1)
        run.assert_not_awaited()


    async def test_action_preview_keeps_an_earlier_action_result_reference(self):
        reference = {"step": "created", "path": ["target_id"]}
        async def create(context, arguments):
            context.state.proposed_changes.append(PreparedAction(
                "synthetic_create", {},
                ChangePreview(("Create a target",), AsyncMock(return_value=True)),
                AsyncMock(return_value=ActionOutcome("complete", result={"target_id": 7})),
            ))
            return {"status": "confirmation_required"}
        async def use(context, arguments):
            self.assertEqual(arguments["target_id"], reference)
            async def bind(results):
                return PreparedAction(
                    "synthetic_use", {"target_id": results["created"]["target_id"]},
                    ChangePreview(("Use the created target",), AsyncMock(return_value=True)),
                    AsyncMock(return_value=ActionOutcome("complete")),
                )
            context.state.proposed_changes.append(PreparedAction(
                "synthetic_use", dict(arguments),
                ChangePreview(("Use the target created above",),
                              AsyncMock(return_value=True)),
                AsyncMock(), bind=bind,
            ))
            return {"status": "confirmation_required"}
        tools = {
            "synthetic_create": RegisteredAgentTool(AgentToolDefinition(
                name="synthetic_create", description="Create a target.",
                parameters={"type": "object", "properties": {}, "required": [],
                            "additionalProperties": False},
            ), create, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
                contract=CapabilityContract(())),
            "synthetic_use": RegisteredAgentTool(AgentToolDefinition(
                name="synthetic_use", description="Use a target.",
                parameters={"type": "object", "properties": {
                    "target_id": {"type": "integer", "minimum": 1},
                }, "required": ["target_id"], "additionalProperties": False},
            ), use, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE),
        }
        plan = _plan([
            {**_step("created"), "capability": "synthetic_create"},
            {**_step("used", {"target_id": reference}), "capability": "synthetic_use",
             "depends_on": ["created"]},
        ])
        session = _Session([_model_step(plan)], self.events)
        context = replace(_context(), bot=SimpleNamespace(tree=object()))
        with (patch("elbow_helper.agent.engine.service.build_agent_tools",
                    return_value={**self.registry, **tools}),
              patch("elbow_helper.agent.engine.service.build_command_tools",
                    return_value=({}, {}))):
            answer = await AgentService(_Model(session)).answer(
                question="synthetic request", local_context="", context=context,
            )
        self.assertIn("Use the target created above", answer)
        self.assertEqual([item.step_id for item in context.state.proposed_changes],
                         ["created", "used"])
        self.assertEqual([item.capability_name for item in context.state.proposed_changes],
                         ["synthetic_create", "synthetic_use"])
        self.assertEqual([item.checked_arguments for item in context.state.proposed_changes],
                         [{}, {"target_id": reference}])
        reference["path"].append("changed")
        self.assertEqual(context.state.proposed_changes[1].checked_arguments,
                         {"target_id": {"step": "created", "path": ["target_id"]}})

    async def test_incomplete_change_preview_cannot_be_confirmed(self):
        path = "/synthetic"
        run = AsyncMock(return_value=ActionOutcome("complete", text="Unexpected"))
        async def prepare(context, values):
            if values["target"] == 202:
                raise ActionRefused("Synthetic target unavailable")
            return ChangePreview((f"Change target {values['target']}",),
                                 AsyncMock(return_value=True))
        command = DiscoveredCommand(path, "registered", (
            ParameterInfo("target", "Select a target.", True, "integer"),
        ))
        help_entry = SimpleNamespace(path=path, summary="Change a value.", details="Uses one target.")
        with (patch("elbow_helper.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, capabilities = build_command_tools(object(), (
                CommandAdapter(path, "confirm", run, prepare=prepare),
            ))
        name = next(iter(tools))
        plan = _plan([{**_step(str(value), {"target": value}), "capability": name}
                      for value in (101, 202)])
        session = _Session([_model_step(plan),
                            AgentStep("Explain unavailable changes.", (), AgentUsage())], self.events)
        context = _context()
        with (patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.agent.engine.service.build_command_tools",
                    return_value=(tools, capabilities))):
            context = replace(context, bot=SimpleNamespace(tree=object()))
            answer = await AgentService(_Model(session)).answer(
                question="synthetic request", local_context="", context=context)
        self.assertEqual(answer, "Explain unavailable changes.")
        self.assertEqual(context.state.proposed_changes, [])
        run.assert_not_awaited()
