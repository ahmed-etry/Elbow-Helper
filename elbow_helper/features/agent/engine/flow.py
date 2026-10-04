"""Request planning and final reply selection."""

from __future__ import annotations

import logging

from dataclasses import replace
from collections.abc import Mapping
import json
import time
from typing import Any
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.infrastructure.ai import AgentToolResult
from elbow_helper.infrastructure.ai import TextGenerationError
from ..actions.contracts import ActionClass
from ..access import require_access, require_evidence_access
from ..plan.checker import check_plan
from ..plan.format import PLAN_TOOL_NAME
from ..plan.planning import read_request
from ..plan.results import plan_feedback
from ..actions.outcomes import command_reply
from ..actions.preview import preview_text
from ..actions.details import prepare_preview
from ..wording import (
    AGENT_ANSWER_UNFINISHED,
    AGENT_PLAN_UNFINISHED,
    AGENT_RESEARCH_UNFINISHED,
)
from . import budgets as limits
from .rounds import AgentGracefulEnd, AgentLimitReached, ModelRounds
from .steps import PlanRunner, disclosure_issue
from collections.abc import Callable
from ..models import AgentRequestContext, RegisteredAgentTool
from .budgets import ContextBudget
from elbow_helper.infrastructure.ai.agent import AgentSession
from ..prompts import (
    RESULT_ANSWER_INSTRUCTION,
    MISSING_VALUES_INSTRUCTION,
    CHANGE_REFUSAL_INSTRUCTION,
    LIMIT_ANSWER_INSTRUCTION,
)

LOGGER = logging.getLogger(__name__)


class AnswerFlow:
    """Read a request, run checked steps, and shape its final reply."""

    def __init__(
        self,
        *,
        context: AgentRequestContext,
        session: AgentSession,
        registry: Mapping[str, RegisteredAgentTool],
        sources: Mapping[str, frozenset[int | str]],
        command_check: Callable[[Mapping[str, Any]], str],
        budget: ContextBudget,
        rounder: ModelRounds,
        runner: PlanRunner,
        request_id: int | None,
    ) -> None:
        self.context = context
        self.session = session
        self.registry = registry
        self.sources = sources
        self.command_check = command_check
        self.budget = budget
        self.rounder = rounder
        self.runner = runner
        self.request_id = request_id
        self.completed_results = {}

    async def run(self) -> str:
        try:
            return await self._run()
        except AgentLimitReached as error:
            if not self.completed_results:
                raise
            return await self._limit_reply(error)

    async def _limit_reply(self, error: AgentLimitReached) -> str:
        pending = self.rounder.pending_results or tuple(
            AgentToolResult(call.call_id, json.dumps({
                "results": self.completed_results,
                "flags": {"status": "refused", "reason": "answer_only"},
            }, ensure_ascii=False, default=str))
            for call in self.rounder.pending_calls
        )
        try:
            reply = await self.rounder.advance(
                pending, allow_tools=False, reasoning_effort=AgentReasoningEffort.LOW,
                max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
                continuation_instruction=LIMIT_ANSWER_INSTRUCTION, final=True,
            )
        except (AgentGracefulEnd, TextGenerationError, TimeoutError):
            return str(error)
        if reply.tool_calls or not reply.content:
            return str(error)
        await require_evidence_access(self.context)
        return reply.content

    async def _run(self) -> str:
        self.context = replace(
            self.context,
            member=require_access(
                self.context.guild,
                self.context.member.id,
                self.context.source_message.channel,
            ),
        )
        await require_evidence_access(self.context)
        self.rounder.context = self.runner.context = self.context
        self.decision = await read_request(
            self.session,
            self.registry,
            self.sources,
            self.request_id,
            validate_step=lambda step: disclosure_issue(self.context, self.registry, step),
            validate_plan=self.command_check,
            advance=self.rounder.advance,
        )
        if self.decision.answer is not None:
            await require_evidence_access(self.context)
            return self.decision.answer
        self.plan = self.decision.plan
        self.revisions = 0
        self._reserve_answer()
        while self.rounder.rounds < limits.MAX_MODEL_ROUNDS:
            results = await self.runner.run(self.plan)
            self.completed_results = results
            self.rounder.results_available = bool(results)
            response = await self._result_response(results)
            if response is not None:
                return response
            response, model_step = await self._answer_round(results)
            if response is not None:
                return response
            response = await self._revise(model_step)
            if response is not None:
                return response
        raise AgentLimitReached(AGENT_RESEARCH_UNFINISHED)

    def _reserve_answer(self) -> None:
        self.budget.final_answer_reserve = limits.ANSWER_OUTPUT_LIMITS[
            AgentReasoningEffort(self.plan["effort"])
        ]

    async def _result_response(self, results: Mapping[str, Any]) -> str | None:
        state = self.context.state
        lookup_missing = [item for item in results.values()
                          if item.get("status") == "needs_input"]
        missing = [item for item in state.outcomes if item.status == "needs_input"]
        if missing or lookup_missing:
            state.proposed_changes.clear()
            options = list({option["name"]: option for item in missing
                            for option in item.missing_options}.values())
            hints = list(dict.fromkeys(
                [description for item in missing for description in item.missing]
                + [description for item in lookup_missing
                   for description in item.get("missing", ())]
            ))
            return await self._feedback_reply(
                results, MISSING_VALUES_INSTRUCTION,
                missing_options=options, missing_hints=hints,
            )
        prepared_steps = {action.step_id for action in state.proposed_changes}
        changes = [step for step in self.plan["steps"]
                   if self.registry[step["capability"]].action_class in (
                       ActionClass.CHANGE, ActionClass.IRREVERSIBLE)]
        if any(step["id"] not in prepared_steps
               or results[step["id"]].get("error") is not None
               or results[step["id"]].get("status") == "no_change"
               or results[step["id"]].get("flags", {}).get("status") in ("failed", "refused")
               for step in changes):
            state.proposed_changes.clear()
            return await self._feedback_reply(results, CHANGE_REFUSAL_INSTRUCTION)
        if state.proposed_changes:
            await prepare_preview(self.context)
            response = preview_text(state.proposed_changes)
            if state.outcomes:
                response += "\n\n" + command_reply(state.outcomes)
            await require_evidence_access(self.context)
            return response
        if state.outcomes:
            await require_evidence_access(self.context)
            return command_reply(state.outcomes)
        return None

    async def _feedback_reply(self, results, instruction, **details) -> str:
        pending = (AgentToolResult(
            self.decision.rounds[-1].tool_calls[0].call_id,
            json.dumps({"results": results, "instruction": instruction, **details},
                       ensure_ascii=False, default=str),
        ),)
        reply = await self.rounder.advance(
            pending, allow_tools=False, reasoning_effort=AgentReasoningEffort.LOW,
            max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
        )
        if reply.tool_calls or not reply.content:
            raise AgentGracefulEnd(AGENT_PLAN_UNFINISHED)
        await require_evidence_access(self.context)
        return reply.content

    async def _answer_round(self, results: Mapping[str, Any]) -> tuple[str | None, Any]:
        pending = (
            AgentToolResult(
                self.decision.rounds[-1].tool_calls[0].call_id,
                json.dumps(
                    {
                        "results": results,
                        "instruction": RESULT_ANSWER_INSTRUCTION,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            ),
        )
        projected = self.budget.projected_input(pending)
        answer_limit = limits.ANSWER_OUTPUT_LIMITS[AgentReasoningEffort(self.plan["effort"])]
        available = (
            answer_limit
            if self.budget.context_window_tokens is None
            else min(answer_limit, self.budget.context_window_tokens - projected)
        )
        if available < limits.MIN_FINAL_OUTPUT_TOKENS:
            self.rounder.pending_results = pending
            raise AgentLimitReached(AGENT_RESEARCH_UNFINISHED)
        self.budget.output_reserve = available
        allow_more = (
            self.rounder.rounds < limits.MAX_MODEL_ROUNDS - 1
            and self.runner.tool_calls < limits.MAX_TOOL_CALLS
            and self.runner.evidence_characters < limits.MAX_EVIDENCE_CHARACTERS
            and self.budget.can_continue_tools(projected, result_reserve=8192)
            and (
                self.context.deadline_monotonic is None
                or time.monotonic()
                + limits.MODEL_ROUND_TIME_RESERVE_SECONDS
                + limits.TOOL_TIMEOUT_SECONDS
                + limits.FINAL_ANSWER_TIME_RESERVE_SECONDS
                + limits.DELIVERY_TIME_RESERVE_SECONDS
                < self.context.deadline_monotonic
            )
        )
        effort = AgentReasoningEffort(self.plan["effort"])
        step = await self.rounder.advance(
            pending,
            allow_tools=allow_more,
            reasoning_effort=effort,
            max_output_tokens=available,
            continuation_instruction=LIMIT_ANSWER_INSTRUCTION if not allow_more else None,
        )
        if not step.tool_calls:
            if not step.content:
                raise AgentGracefulEnd(AGENT_ANSWER_UNFINISHED)
            await require_evidence_access(self.context)
            return step.content, None
        if not allow_more:
            refusals = tuple(
                AgentToolResult(
                    call.call_id,
                    json.dumps({"flags": {"status": "refused", "reason": "answer_only"}}),
                )
                for call in step.tool_calls
            )
            recovery = await self.rounder.advance(
                refusals,
                allow_tools=False,
                reasoning_effort=effort,
                max_output_tokens=available,
                continuation_instruction=LIMIT_ANSWER_INSTRUCTION,
            )
            if recovery.tool_calls or not recovery.content:
                raise AgentGracefulEnd(AGENT_RESEARCH_UNFINISHED)
            await require_evidence_access(self.context)
            return recovery.content, None
        return None, step

    async def _revise(self, model_step: Any) -> str | None:
        if len(model_step.tool_calls) != 1 or model_step.tool_calls[0].name != PLAN_TOOL_NAME:
            raise AgentGracefulEnd(AGENT_PLAN_UNFINISHED)
        try:
            next_plan = json.loads(model_step.tool_calls[0].arguments)
        except (TypeError, ValueError):
            next_plan = None
        check = check_plan(next_plan, self.registry, self.sources)
        if check.ok:
            issue = self.command_check(next_plan)
            if issue:
                check = type(check)(False, issue)
        LOGGER.info(
            "Agent plan: request=%s revision=%s plan=%s",
            self.request_id,
            self.revisions + 1,
            json.dumps(next_plan, ensure_ascii=False, default=str),
        )
        LOGGER.info(
            "Agent plan check: request=%s revision=%s ok=%s step=%s error=%s",
            self.request_id,
            self.revisions + 1,
            check.ok,
            check.step_id,
            check.error,
        )
        if not check.ok:
            corrected = await self.rounder.advance(
                (
                    AgentToolResult(
                        model_step.tool_calls[0].call_id,
                        json.dumps(
                            plan_feedback(check.error, step_id=check.step_id, offered=check.offered)
                        ),
                    ),
                ),
                reasoning_effort=AgentReasoningEffort(self.plan["effort"]),
            )
            if not corrected.tool_calls and corrected.content:
                return corrected.content
            if len(corrected.tool_calls) != 1 or corrected.tool_calls[0].name != PLAN_TOOL_NAME:
                raise AgentGracefulEnd(AGENT_PLAN_UNFINISHED)
            try:
                next_plan = json.loads(corrected.tool_calls[0].arguments)
            except (TypeError, ValueError):
                next_plan = None
            check = check_plan(next_plan, self.registry, self.sources)
            if check.ok:
                issue = self.command_check(next_plan)
                if issue:
                    check = type(check)(False, issue)
            LOGGER.info(
                "Agent plan correction: request=%s ok=%s plan=%s error=%s",
                self.request_id,
                check.ok,
                json.dumps(next_plan, ensure_ascii=False),
                check.error,
            )
            if not check.ok:
                raise AgentGracefulEnd(AGENT_PLAN_UNFINISHED)
            model_step = corrected
        self.revisions += 1
        self.plan = next_plan
        self._reserve_answer()
        self.decision = type(self.decision)(None, self.plan, (model_step,))
        return None
