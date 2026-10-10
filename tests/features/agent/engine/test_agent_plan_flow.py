"""Plan flow with synthetic capabilities and model responses."""

from __future__ import annotations

from features.agent.engine.helpers import patch_engine

import json
from contextlib import nullcontext
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
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.service import AgentUnavailableError
from elbow_helper.features.agent.commands.bridge import build_command_tools
from elbow_helper.features.agent.actions.outcomes import ActionOutcome
from elbow_helper.features.agent.actions.contracts import ActionRefused, ChangePreview
from elbow_helper.features.agent.actions.contracts import ActionClass, PreparedAction
from elbow_helper.features.agent.commands.registry import CommandAdapter
from elbow_helper.features.agent.wording import (
    AGENT_ANSWER_UNFINISHED, AGENT_PLAN_UNFINISHED,
)
from elbow_helper.features.help.discovery import DiscoveredCommand, ParameterInfo
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.engine.capability_contract import CapabilityContract
from elbow_helper.features.agent.engine.budgets import ContextBudget
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentToolDefinition, AgentUsage
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.infrastructure.ai import TextGenerationError

from features.agent.engine.helpers import patch_contracts


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
    def __init__(self, session):
        self.session = session
        self.request = None

    def create_agent_session(self, **kwargs):
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
        corrected = _plan([_step("corrected", {"value": 17})])
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

    async def test_bad_reference_structure_uses_plan_correction(self):
        invalid = _plan([_step("first"), _step("second", {
            "value": {"step": "first", "path": []},
        }, ["first"])])
        session = _Session([
            _model_step(invalid), _model_step(_plan([_step("corrected")])),
            AgentStep("Synthetic answer", (), AgentUsage()),
        ], self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Synthetic answer")
        self.assertEqual(self.events, ["model", "model", "read", "model"])
        self.assertIn("rule", session.calls[1][0][0].content)

    async def test_reply_order_tracks_plan_order_while_reads_still_execute_first(self):
        async def prepare(context, arguments):
            self.events.append("change")
            context.state.proposed_changes.append(PreparedAction(
                "synthetic_change", {}, ChangePreview(("Synthetic preview",), AsyncMock(return_value=True)),
                AsyncMock(),
            ))
            return {"status": "confirmation_required"}

        self.registry["synthetic_change"] = RegisteredAgentTool(AgentToolDefinition(
            "synthetic_change", "Change a synthetic value.",
            {"type": "object", "properties": {}, "required": []}),
            prepare, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
        )
        for preview_first in (True, False):
            with self.subTest(preview_first=preview_first):
                self.events.clear()
                change = {**_step("change"), "capability": "synthetic_change"}
                read = _step("lookup")
                plan = _plan([change, read] if preview_first else [read, change])
                context = replace(_context(), bot=SimpleNamespace(tree=object()))
                session = _Session([_model_step(plan), AgentStep("Synthetic answer", (), AgentUsage())], self.events)
                with patch("elbow_helper.features.agent.engine.service.build_command_tools", return_value=({}, {})):
                    answer, _ = await self._answer(session, context)
                self.assertEqual(answer, "Synthetic answer")
                self.assertEqual(context.state.preview_first, preview_first)
                self.assertEqual(self.events, ["model", "read", "change", "model"])

    async def test_compact_pages_preserve_payload_references_evidence_and_report(self):
        report = SimpleNamespace(report_id="synthetic-report", retained_bytes=100,
                                 manifest=lambda: {"report_id": "synthetic-report"})

        async def page(context, arguments):
            context.state.reports[report.report_id] = report
            offset = arguments.get("offset", 0)
            return {"report_id": report.report_id, "summary": {"total": 20},
                    "players": [{"target_id": 101 + index, "name": f"Synthetic {index}"}
                                for index in range(offset, offset + 10)],
                    "next_offset": 10 if offset == 0 else None}

        consume = AsyncMock(return_value={"value": 7})
        self.registry = {
            "synthetic_page": RegisteredAgentTool(AgentToolDefinition(
                "synthetic_page", "Read synthetic values.", {"type": "object", "properties": {
                    "report_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                }, "required": []}), page, contract=CapabilityContract(
                    (("report_id", "synthetic_report"),), retained_fields=("report_id",),
                )),
            "synthetic_consume": RegisteredAgentTool(AgentToolDefinition(
                "synthetic_consume", "Use synthetic values.", {"type": "object", "properties": {
                    "targets": {"type": "array", "items": {"type": "integer"}, "maxItems": 20},
                }, "required": ["targets"]}), consume, contract=CapabilityContract(
                    (("targets", "synthetic_target_set"),),
                )),
        }
        plan = _plan([
            {**_step("first"), "capability": "synthetic_page"},
            {**_step("second", {"report_id": {"step": "first", "path": ["report_id"]},
                               "offset": {"step": "first", "path": ["next_offset"]}}, ["first"]),
             "capability": "synthetic_page"},
            {**_step("consume", {"targets": {"step": "second", "path": ["players", "*", "target_id"]}},
                     ["second"]), "capability": "synthetic_consume"},
        ])
        context = _context()
        session = _Session([_model_step(plan), AgentStep("Synthetic answer", (), AgentUsage())], self.events)
        answer, _ = await self._answer(session, context)
        self.assertEqual(answer, "Synthetic answer")
        consume.assert_awaited_once()
        self.assertEqual(consume.await_args.args[1]["targets"], list(range(111, 121)))
        results = json.loads(session.calls[-1][0][0].content)["results"]
        self.assertEqual(results["first"]["players"]["columns"], ["target_id", "name"])
        self.assertIn("summary", results["first"])
        self.assertNotIn("summary", results["second"])
        self.assertEqual(results["second"]["players"]["rows"][0], [111, "Synthetic 10"])
        evidence = json.loads(json.loads(context.state.evidence[1])["result"])
        self.assertIn("summary", evidence)
        self.assertEqual(evidence["players"][0]["target_id"], 111)
        self.assertIs(context.state.reports[report.report_id], report)

        consume.reset_mock()
        continuation = _plan([
            {**plan["steps"][1], "arguments": {"report_id": report.report_id, "offset": 10},
             "depends_on": []}, plan["steps"][2],
        ])
        session = _Session([_model_step(_plan([plan["steps"][0]])), _model_step(continuation),
                            AgentStep("Synthetic answer", (), AgentUsage())], self.events)
        context = _context()
        await self._answer(session, context)
        first = json.loads(session.calls[1][0][0].content)["results"]["first"]
        second = json.loads(session.calls[2][0][0].content)["results"]["second"]
        self.assertIn("summary", first)
        self.assertNotIn("summary", second)
        self.assertEqual(consume.await_args.args[1]["targets"], list(range(111, 121)))

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
                    logs = (self.assertLogs("elbow_helper.features.agent.engine.steps", level="ERROR")
                            if mismatch else nullcontext())
                    with (patch("elbow_helper.features.agent.engine.service.build_agent_tools",
                                return_value={"synthetic_change": tool}),
                          patch("elbow_helper.features.agent.engine.service.build_command_tools",
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

    async def test_results_receive_a_final_answer_at_output_limits(self):
        session = _Session([
            _model_step(_plan([_step("first")])),
            AgentStep("Part one", (), AgentUsage(), output_limit_reached=True),
            AgentStep("Checked seven; no more reads.", (), AgentUsage()),
        ], self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Checked seven; no more reads.")
        self.assertFalse(session.calls[-1][1])
        self.assertEqual(session.instructions[-1],
                         "Answer now from these results. Say briefly what you couldn't finish.")
        self.assertEqual(self.events.count("read"), 1)

    async def test_small_remaining_context_still_answers_from_checked_results(self):
        session = _Session([_model_step(_plan([_step("first")])),
                            AgentStep("Checked seven.", (), AgentUsage())], self.events)
        projected = ContextBudget.projected_input
        def pressure(budget, results):
            if results:
                return budget.context_window_tokens - 512
            return projected(budget, results)
        with patch.object(ContextBudget, "projected_input", pressure):
            answer, _ = await self._answer(session)
        self.assertEqual(answer, "Checked seven.")
        self.assertFalse(session.calls[-1][1])
        self.assertEqual(session.instructions[-1],
                         "Answer now from these results. Say briefly what you couldn't finish.")
        self.assertLessEqual(session.output_limits[-1], 512)

    async def test_failed_final_answer_uses_the_fixed_unfinished_message(self):
        session = _Session([_model_step(_plan([_step("first")])),
                            AgentStep("Part one", (), AgentUsage(), output_limit_reached=True)],
                           self.events)
        advance = session.advance
        async def fail_final(*args, **kwargs):
            if kwargs.get("continuation_instruction") == (
                "Answer now from these results. Say briefly what you couldn't finish."
            ):
                raise TextGenerationError("synthetic_failure")
            return await advance(*args, **kwargs)
        session.advance = fail_final
        answer, _ = await self._answer(session)
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
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
        with patch("elbow_helper.features.agent.engine.service.build_agent_tools",
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

    async def test_second_output_limit_ends_with_a_short_note(self):
        session = _Session([
            AgentStep("First part", (), AgentUsage(), output_limit_reached=True),
            AgentStep("Second part", (), AgentUsage(), output_limit_reached=True),
        ], self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
        self.assertNotIn("First part", answer)

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

    async def test_initial_plan_and_each_revision_get_one_correction(self):
        initial = _plan([_step("initial", {"value": 1})])
        invalid_initial = {**initial, "output": "unavailable"}
        first_revision = _plan([_step("revision_one", {"value": 2})])
        second_revision = _plan([_step("revision_two", {"value": 3})])
        session = _Session([
            _model_step(invalid_initial), _model_step(initial),
            _model_step({**first_revision, "output": "unavailable"}),
            _model_step(first_revision),
            _model_step({**second_revision, "output": "unavailable"}),
            _model_step(second_revision),
            AgentStep("Synthetic request complete.", (), AgentUsage()),
        ], self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Synthetic request complete.")
        self.assertEqual(self.events.count("read"), 3)
        self.assertEqual(len(session.calls), 7)
        for index in (1, 3, 5):
            self.assertIn("rule", session.calls[index][0][0].content)
        self.assertEqual(json.loads(session.calls[-1][0][0].content)["results"]["revision_two"]["value"], 3)

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
                self.assertEqual(answer, AGENT_PLAN_UNFINISHED)
                self.assertEqual(self.events.count("read"), 1)
                self.assertEqual(len(session.calls), 3)


    async def test_max_effort_is_reserved_for_the_answer_round(self):
        plan = _plan([_step("first")], effort="max")
        session = _Session([_model_step(plan), AgentStep("Seven.", (), AgentUsage())],
                           self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Seven.")
        self.assertEqual([call[2] for call in session.calls],
                         [AgentReasoningEffort.LOW, AgentReasoningEffort.MAX])

    async def test_answer_output_limit_grows_with_effort(self):
        for effort, expected in (("low", 16_000), ("high", 32_000),
                                 ("max", 64_000)):
            with self.subTest(effort=effort):
                session = _Session([
                    _model_step(_plan([_step("first")], effort=effort)),
                    AgentStep("Seven.", (), AgentUsage()),
                ], self.events)
                limits = []
                advance = session.advance

                async def capture(*args, **kwargs):
                    limits.append(kwargs["max_output_tokens"])
                    return await advance(*args, **kwargs)

                session.advance = capture
                await self._answer(session)
                self.assertEqual(limits, [8_000, expected])
                self.events.clear()

    async def test_answer_output_limit_stays_inside_context(self):
        session = _Session([
            _model_step(_plan([_step("first")], effort="max")),
            AgentStep("Seven.", (), AgentUsage()),
        ], self.events)
        session.context_window_tokens = 70_000
        limits = []
        advance = session.advance

        async def capture(*args, **kwargs):
            limits.append(kwargs["max_output_tokens"])
            return await advance(*args, **kwargs)

        session.advance = capture
        await self._answer(session)
        self.assertLess(limits[1], 64_000)
        self.assertGreaterEqual(limits[1], 1_024)

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

    async def test_six_plans_can_complete(self):
        self.registry["read_value"] = replace(self.registry["read_value"], definition=AgentToolDefinition(
            "read_value", "Read a value.", {"type": "object", "properties": {
                "period": {"type": "integer", "minimum": 0}}, "required": ["period"]}))
        plans = [_plan([_step(f"period_{n}", {"period": n})])
                 for n in range(6)]
        session = _Session([*[_model_step(plan) for plan in plans],
                            AgentStep("Checked six periods.", (), AgentUsage())], self.events)
        with patch_contracts(self.registry, {
            "read_value": CapabilityContract(())
        }):
            answer, _ = await self._answer(session)
        self.assertEqual(answer, "Checked six periods.")
        self.assertEqual(len(session.calls), 7)
        self.assertEqual(self.events.count("read"), 6)

    async def test_answer_only_round_refuses_extra_calls_once(self):
        plan = _plan([_step("first")])
        extra = AgentToolCall("extra", "submit_request_plan", json.dumps(plan))
        session = _Session([
            _model_step(plan), AgentStep("", (extra,), AgentUsage()),
            AgentStep("Seven.", (), AgentUsage()),
        ], self.events)
        with patch("elbow_helper.features.agent.engine.budgets.MAX_MODEL_ROUNDS", 2):
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
                         ["complete", "complete"])

    async def test_revised_plan_references_completed_read(self):
        first = _plan([_step("first", {"value": 7})])
        second = _plan([
            _step("second", {"value": {"step": "first", "path": ["value"]}}, ["first"]),
        ])
        session = _Session([
            _model_step(first), _model_step(second), AgentStep("Done.", (), AgentUsage()),
        ], self.events)
        answer, _ = await self._answer(session)
        self.assertEqual(answer, "Done.")
        self.assertEqual(self.events.count("read"), 1)
        self.assertEqual(
            json.loads(session.calls[-1][0][0].content)["results"]["second"]["value"], 7,
        )

    async def test_lookup_exception_returns_data_and_can_be_corrected(self):
        original = self.registry["read_value"]
        async def read(context, arguments):
            if arguments.get("value") == 1:
                raise AttributeError("Synthetic failure")
            return {"value": arguments.get("value")}
        self.registry["read_value"] = replace(original, handler=read)
        session = _Session([_model_step(_plan([_step("first", {"value":1})])),
            _model_step(_plan([_step("second", {"value":2})])),
            AgentStep("Corrected.", (), AgentUsage())], self.events)
        with self.assertLogs("elbow_helper.features.agent.engine.tool_call", level="ERROR"):
            answer, _ = await self._answer(session)
        self.assertEqual(answer, "Corrected.")
        self.assertIn("error", json.loads(session.calls[1][0][0].content)["results"]["first"])
        self.assertEqual(
            json.loads(session.calls[2][0][0].content)["results"]["second"]["value"], 2,
        )

    async def test_identical_outputs_are_not_deduplicated(self):
        original = self.registry["read_value"]
        self.registry["read_value"] = replace(original, action_class=ActionClass.OUTPUT)
        session = _Session([_model_step(_plan([_step("first", {"value":7})])),
            _model_step(_plan([_step("second", {"value":7})])),
            AgentStep("Done.", (), AgentUsage())], self.events)
        await self._answer(session)
        self.assertEqual(self.events.count("read"), 2)

    async def test_revised_plan_requires_new_step_id(self):
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

    async def test_disclosure_rejection_is_returned_to_the_model(self):
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
            (("channel_id", "discord_channel"),), channel_fields=("channel_id",),
            source_scope="channel_messages", result_channel_fields=("channel_id",),
        )
        plan = _plan([_step("first", {"channel_id": 202})])
        session = _Session([
            _model_step(plan), AgentStep("That conversation is unavailable.", (), AgentUsage()),
        ], self.events)
        with (
            patch_contracts(self.registry,
                       {"read_value": contract}),
            patch("elbow_helper.features.agent.engine.steps.accessible_message_channel",
                  return_value=None),
        ):
            answer, _ = await self._answer(session)
        self.assertEqual(answer, "That conversation is unavailable.")
        self.assertIn("cannot access", session.calls[1][0][0].content)
        self.assertEqual(len(session.calls), 2)
        self.assertNotIn("read", self.events)

    async def test_unexpected_value_error_is_not_reported_as_an_unsettled_plan(self):
        session = _Session([], self.events)
        with (
            patch("elbow_helper.features.agent.engine.flow.read_request",
                  AsyncMock(side_effect=ValueError("broken lookup"))),
            self.assertRaisesRegex(ValueError, "broken lookup"),
        ):
            await self._answer(session)

    async def test_exhausted_context_does_not_run_tools_or_send_another_provider_request(self):
        session = _Session([_model_step(_plan([_step("first")]))], self.events)
        session.context_window_tokens = 100
        answer, _ = await self._answer(session)
        self.assertIn("couldn't finish checking", answer)
        self.assertEqual(self.events, [])

    async def test_interrupted_provider_round_is_logged_as_unknown_usage(self):
        session = _Session([], self.events)

        async def fail(*args, **kwargs):
            raise TextGenerationError("synthetic_failure")

        session.advance = fail
        with self.assertLogs("elbow_helper.features.agent.engine", level="INFO") as logs:
            with self.assertRaises(AgentUnavailableError):
                await self._answer(session)
        self.assertTrue(any("attempted_rounds=1" in line and "unknown_token_rounds=1" in line
                            for line in logs.output))

    async def test_success_without_usage_is_not_recorded_as_known_free(self):
        session = _Session([AgentStep("Ready.", (), AgentUsage())], self.events)
        with self.assertLogs("elbow_helper.features.agent.engine", level="INFO") as logs:
            await self._answer(session)
        self.assertTrue(any("unknown_token_rounds=1" in line and "unknown_cache_rounds=1" in line
                            for line in logs.output))

    async def test_round_and_tool_diagnostics_include_identity_and_duration(self):
        reply = AgentStep("Ready.", (), AgentUsage(100, 50, 80, 20),
                          "provider-id", "synthetic-model", 7)
        session = _Session([_model_step(_plan([_step("first")])), reply], self.events)
        with self.assertLogs("elbow_helper.features.agent.engine", level="INFO") as logs:
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
        with patch("elbow_helper.features.agent.engine.budgets.MAX_TOOL_CALLS", 1):
            await self._answer(session)
        self.assertFalse(session.calls[1][1])
        self.assertEqual(self.events, ["model", "read", "model"])

    async def test_final_answer_recovery_does_not_loop(self):
        plan = _plan([_step("first")])
        session = _Session([_model_step(plan)] * 3, self.events)
        with patch("elbow_helper.features.agent.engine.budgets.MAX_MODEL_ROUNDS", 2):
            answer, _ = await self._answer(session)
        self.assertIn("couldn't finish checking", answer)
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
        with self.assertLogs("elbow_helper.features.agent.engine.steps", level="WARNING") as logs:
            await self._answer(session)
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertEqual(logs.records[0].getMessage(),
                         "Agent planned step refused: step=second capability=read_value "
                         'error=Invalid arguments: value must match {"type":"integer"}')
        self.assertNotIn("wrong-type", " ".join(logs.output))
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

    async def test_context_pressure_requests_final_answer_without_lowering_output_limit(self):
        session = _Session([_model_step(_plan([_step("first")])), AgentStep("Ready.", (), AgentUsage())], self.events)
        calls = []
        advance = session.advance
        async def observe(*args, **kwargs):
            calls.append(kwargs)
            return await advance(*args, **kwargs)
        session.advance = observe
        with patch("elbow_helper.features.agent.engine.service.ContextBudget.can_continue_tools", return_value=False):
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
                with patch("elbow_helper.features.agent.engine.budgets." + budget, value):
                    await self._answer(session)
                self.assertFalse(session.calls[1][1])
                self.assertFalse(session.calls[2][1])
                self.assertEqual(session.instructions[2],
                    "Answer now from these results. Say briefly what you couldn't finish.")
                self.assertEqual(self.events.count("read"), 1)
                self.assertIn("answer_only", session.calls[2][0][0].content)

    async def test_dependent_period_is_bound_to_the_owner_result(self):
        contract = CapabilityContract(())
        async def read(_, arguments):
            self.events.append(arguments)
            return {"key": "synthetic-key", "value": 7}
        self.registry["read_value"] = replace(self.registry["read_value"],
            definition=AgentToolDefinition("read_value", "Read a value.", {
                "type": "object", "properties": {"selected_key": {"type": "string"}}}), handler=read)
        period = {"kind": "resolved", "step": "first", "selector": "latest", "path": ["key"]}
        plan = _plan([_step("first"), _step("second", {"selected_key": {"step": "first", "path": ["key"]}}, ["first"])], periods=[period])
        session = _Session([_model_step(plan), AgentStep("Ready.", (), AgentUsage())], self.events)
        with patch_contracts(self.registry, {"read_value": contract}):
            await self._answer(session)
        self.assertEqual(self.events, ["model", {}, {"selected_key": "synthetic-key"}, "model"])

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
              patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.features.agent.engine.budgets.MAX_TOOL_CALLS", 1)):
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

    async def test_distinct_reads_do_not_limit_plan_revisions(self):
        self.registry["read_value"] = replace(
            self.registry["read_value"], contract=CapabilityContract(()),
        )
        plans = [_plan([_step(str(index), {"value": index + 1})]) for index in range(5)]
        session = _Session([*map(_model_step, plans), AgentStep("Ready.", (), AgentUsage())], self.events)
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
        with patch("elbow_helper.features.agent.engine.budgets.MAX_TOOL_CALLS", 2):
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
        with patch_engine("time", SimpleNamespace(monotonic=lambda: clock.value)):
            async def advance_clock(*args, **kwargs):
                result = await advance(*args, **kwargs)
                clock.value = now + 150
                return result
            session.advance = advance_clock
            await self._answer(session, context)
        self.assertNotIn("read", self.events)
        self.assertEqual(len(session.calls), 2)


    async def test_disabled_commands_keep_the_read_only_catalogue(self):
        session = _Session([AgentStep("Ready.", (), AgentUsage())], self.events)
        with patch("elbow_helper.features.agent.engine.service.build_command_tools") as commands:
            _, model = await self._answer(session)
        commands.assert_not_called()
        self.assertIn(
            "You cannot browse the internet, remember other conversations unless they are "
            "supplied, or change anything", model.request["system_prompt"],
        )

    async def test_enabled_command_asks_once_then_runs_on_reply(self):
        run = AsyncMock(return_value=ActionOutcome("complete", text="Synthetic result"))
        path = "/synthetic"
        command = DiscoveredCommand(
            path, "registered", (ParameterInfo(
                "value", "A required value.", True, "integer"),))
        help_entry = SimpleNamespace(path=path, summary="Get a synthetic result.", details="Uses a value.")
        with (patch(
            "elbow_helper.features.agent.commands.registry.discover_commands",
            return_value={path: command},
        ),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, capabilities = build_command_tools(object(), (CommandAdapter(path, "public", run),))
        command_name = next(iter(tools))
        async def answer(plan, history=""):
            session = _Session([
                _model_step(plan), AgentStep("Which value should I use?", (), AgentUsage()),
            ], self.events)
            model = _Model(session)
            context = _context()
            with (patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=self.registry),
                  patch("elbow_helper.features.agent.engine.service.build_command_tools",
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
        self.assertIn("Do the task yourself with your capabilities", model.request["system_prompt"])
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
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={path: DiscoveredCommand(path, "registered", ())}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, capabilities = build_command_tools(object(), (CommandAdapter(path, "private", run),))
        plan = _plan([{**_step("command"), "capability": next(iter(tools))}])
        session = _Session([_model_step(plan)], self.events)
        context = _context()
        with (patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.features.agent.engine.service.build_command_tools",
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
        with (patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.features.agent.engine.service.build_command_tools",
                    return_value=(tools, capabilities))):
            context = replace(context, bot=SimpleNamespace(tree=object()))
            answer = await AgentService(_Model(session)).answer(
                question="synthetic request", local_context="", context=context)
        self.assertIn("Change target 101", answer)
        self.assertIn("Change target 202", answer)
        self.assertEqual(len(context.state.proposed_changes), 2)
        self.assertEqual(len(session.calls), 1)
        run.assert_not_awaited()

    async def test_one_action_step_can_preview_each_selected_target(self):
        run = AsyncMock(return_value=ActionOutcome("complete"))
        async def prepare(context, arguments):
            for target in arguments["targets"]:
                context.state.proposed_changes.append(PreparedAction(
                    "synthetic_change", {"target": target},
                    ChangePreview((f"Change target {target}",), AsyncMock(return_value=True)),
                    run,
                ))
            return {"status": "confirmation_required"}
        tool = RegisteredAgentTool(AgentToolDefinition(
            name="synthetic_change", description="Change selected targets.",
            parameters={"type": "object", "properties": {
                "targets": {"type": "array", "items": {"type": "integer", "minimum": 1},
                            "minItems": 1, "maxItems": 5},
            }, "required": ["targets"], "additionalProperties": False},
        ), prepare, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE)
        plan = _plan([{**_step("many", {"targets": [101, 202]}),
                       "capability": "synthetic_change"}])
        session = _Session([_model_step(plan)], self.events)
        context = replace(_context(), bot=SimpleNamespace(tree=object()))
        with (patch("elbow_helper.features.agent.engine.service.build_agent_tools",
                    return_value={**self.registry, "synthetic_change": tool}),
              patch("elbow_helper.features.agent.engine.service.build_command_tools",
                    return_value=({}, {}))):
            answer = await AgentService(_Model(session)).answer(
                question="synthetic request", local_context="", context=context,
            )
        self.assertIn("Change target 101", answer)
        self.assertIn("Change target 202", answer)
        self.assertEqual(len(context.state.proposed_changes), 2)
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
        with (patch("elbow_helper.features.agent.engine.service.build_agent_tools",
                    return_value={**self.registry, **tools}),
              patch("elbow_helper.features.agent.engine.service.build_command_tools",
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
        with (patch("elbow_helper.features.agent.commands.registry.discover_commands",
                    return_value={path: command}),
              patch("elbow_helper.features.agent.commands.registry.HELP_ENTRIES", (help_entry,))):
            tools, capabilities = build_command_tools(object(), (
                CommandAdapter(path, "confirm", run, prepare=prepare),
            ))
        name = next(iter(tools))
        plan = _plan([{**_step(str(value), {"target": value}), "capability": name}
                      for value in (101, 202)])
        session = _Session([_model_step(plan),
                            AgentStep("Explain unavailable changes.", (), AgentUsage())], self.events)
        context = _context()
        with (patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=self.registry),
              patch("elbow_helper.features.agent.engine.service.build_command_tools",
                    return_value=(tools, capabilities))):
            context = replace(context, bot=SimpleNamespace(tree=object()))
            answer = await AgentService(_Model(session)).answer(
                question="synthetic request", local_context="", context=context)
        self.assertEqual(answer, "Explain unavailable changes.")
        self.assertEqual(context.state.proposed_changes, [])
        run.assert_not_awaited()
