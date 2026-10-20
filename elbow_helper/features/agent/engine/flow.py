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
from ..plan.checker import check_plan, step_dependencies
from ..plan.format import PLAN_TOOL_NAME
from ..plan.planning import read_request, social_reply_issue
from ..plan.results import plan_feedback
from ..knowledge.answers import needs_private_knowledge, private_knowledge_answer
from ..actions.outcomes import command_reply
from ..actions.preview import preview_text
from ..actions.details import prepare_preview
from ..wording import (
    AGENT_ANSWER_UNFINISHED,
    AGENT_PLAN_UNFINISHED,
    AGENT_RESEARCH_UNFINISHED,
    FAILURE_MESSAGE,
)
from . import budgets as limits
from .rounds import AgentGracefulEnd, AgentLimitReached, ModelRounds
from .steps import PlanRunner
from .grounding import (
    GROUNDING_INSTRUCTION, GroundingError, ground_answer, partial_answer,
)
from .factual_answers import narrow_answer
from .strict_answers import strict_mode
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from .budgets import ContextBudget
from elbow_helper.infrastructure.ai.agent import AgentSession
from ..prompts import (
    RESULT_ANSWER_INSTRUCTION,
    MISSING_VALUES_INSTRUCTION,
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
        budget: ContextBudget,
        rounder: ModelRounds,
        runner: PlanRunner,
        request_id: int | None,
    ) -> None:
        self.context = context
        self.session = session
        self.registry = registry
        self.budget = budget
        self.rounder = rounder
        self.runner = runner
        self.request_id = request_id
        self.completed_results = {}
        self.result_views = {}
        self.read_results = {}
        self.steps_by_id = {}
        self.append_commands = False
        self.failed_reads = set()

    def _model_results(self, results):
        visible = {key: self.result_views[key] for key in results if key in self.result_views}
        self.context.state.grounding_evidence.update(visible)
        return visible

    async def run(self) -> str:
        try:
            answer = await self._run()
        except AgentLimitReached as error:
            if not self.completed_results:
                raise
            answer = await self._limit_reply(error)
        if needs_private_knowledge(answer):
            answer = await private_knowledge_answer(self, answer)
        answer = await self._grounded_reply(answer)
        if needs_private_knowledge(answer):
            # A grounding rewrite is another model output boundary. It cannot
            # release internal protocol or start a second private-answer pass.
            raise AgentGracefulEnd(FAILURE_MESSAGE)
        if self.append_commands and not self.context.state.reply_delivery.posts:
            answer = "\n\n".join(filter(None, (answer, command_reply(self.context.state.outcomes))))
        return answer

    async def _grounded_reply(self, answer):
        """All final paths, including direct, recovery and limit replies, pass here."""
        await require_evidence_access(self.context)
        if not answer:
            return answer
        try:
            factual = narrow_answer(self.context.state.request_text, self.context.state.grounding_evidence)
            if factual is not None:
                return factual
            return ground_answer(answer, self.context.state.grounding_evidence)
        except GroundingError as error:
            failure = error
            LOGGER.info("Agent answer source issue: request=%s kind=%s reason=%s",
                        self.request_id, error.kind, error)
            # A rewrite cannot manufacture missing or ambiguous typed data.
            if error.kind != "source":
                return partial_answer(answer, self.context.state.grounding_evidence, error)
        try:
            repaired = await self.rounder.advance(
                (), allow_tools=False, reasoning_effort=AgentReasoningEffort.LOW,
                max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
                continuation_instruction=GROUNDING_INSTRUCTION +
                f" A source reference could not be resolved ({failure}). "
                "Correct the reference using evidence already read, or remove that claim and "
                "reconsider conclusions relying on it. Preserve the rest of the useful answer "
                "and its requested format. Do not invent a source or withdraw supported recommendations.",
            )
            await require_evidence_access(self.context)
            if repaired.content and not repaired.tool_calls:
                factual = narrow_answer(self.context.state.request_text, self.context.state.grounding_evidence)
                if factual is not None:
                    return factual
                return ground_answer(repaired.content, self.context.state.grounding_evidence)
        except GroundingError as error:
            failure = error
            LOGGER.info("Agent source repair incomplete: request=%s kind=%s reason=%s",
                        self.request_id, error.kind, error)
        except (AgentGracefulEnd, AgentLimitReached, TextGenerationError, TimeoutError) as error:
            LOGGER.info("Agent source repair unavailable: request=%s reason=%s",
                        self.request_id, type(error).__name__)
        await require_evidence_access(self.context)
        return partial_answer(answer, self.context.state.grounding_evidence, failure)

    async def _limit_reply(self, error: AgentLimitReached) -> str:
        if self.context.state.reply_delivery.posts:
            return ""
        pending = self.rounder.pending_results or tuple(
            AgentToolResult(call.call_id, json.dumps({
                "results": self._model_results(self.completed_results),
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
            self.request_id,
            advance=self.rounder.advance,
            on_check=(self.context.operator_debug.checked
                      if self.context.operator_debug is not None else None),
            reasoning_effort=(AgentReasoningEffort.LOW if self.rounder.lookup_route or self.rounder.social_route
                              else AgentReasoningEffort.HIGH),
        )
        if self.decision.answer is not None:
            if (self.rounder.lookup_route and self.rounder.factual_lookup and self.rounder.last_allow_tools
                    and {"read_clash", "read_linked_accounts"}.intersection(self.registry)):
                # A lookup cannot establish missing links or unavailable data by
                # declining before using its resolution tools. Give it one
                # bounded chance to do the work in the original request.
                async def recover(*args, **kwargs):
                    return await self.rounder.advance(*args, **kwargs,
                        continuation_instruction=(
                            "This is a factual lookup with no completed reads in this request. "
                            "Use the available member/account resolution and compact data tools "
                            "before claiming a tag or fact is unavailable. Resolve the original "
                            "request now; ask only for information these tools cannot establish."
                        ))
                self.decision = await read_request(
                    self.session, self.registry, self.request_id, advance=recover,
                    on_check=(self.context.operator_debug.checked if self.context.operator_debug else None),
                    reasoning_effort=AgentReasoningEffort.LOW,
                )
            if self.decision.answer is not None:
                await require_evidence_access(self.context)
                return self.decision.answer
        self.plan = self.decision.plan
        self.revisions = 0
        self._reserve_answer()
        while self.rounder.rounds < limits.MAX_MODEL_ROUNDS:
            results = await self.runner.run(self.plan)
            self.steps_by_id.update({step["id"]: step for step in self.plan["steps"]})
            self.completed_results.update(results)
            self.failed_reads.update(
                step["capability"] + json.dumps(step["arguments"], sort_keys=True, default=str)
                for step in self.plan["steps"] if results.get(step["id"], {}).get("cause")
                in ("invalid_arguments", "sql_schema_error")
            )
            self.result_views.update(self.runner.model_results_for(results))
            self.read_results.update({step["id"]: results[step["id"]]
                                      for step in self.plan["steps"]
                                      if step["id"] in results
                                      and self.registry[step["capability"]].action_class is ActionClass.READ
                                      and self.registry[step["capability"]].effect is AgentCapabilityEffect.READ})
            self.rounder.results_available = bool(results)
            social_steps = self.plan["steps"]
            reply_text = self.decision.rounds[-1].content or ""
            if (social_steps and all(step["capability"] in ("react_to_request", "find_gif") for step in social_steps)
                    and sum(step["capability"] == "find_gif" for step in social_steps) <= 1
                    and (reply_text.strip() or any(
                        step["capability"] == "find_gif" and results.get(step["id"], {}).get("url")
                        for step in social_steps))):
                await require_evidence_access(self.context)
                return reply_text
            response = await self._result_response(results)
            if response is not None:
                return response
            response, model_step = await self._answer_round(
                self.read_results if self.append_commands else results,
            )
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
        self.append_commands = False
        lookup_missing = [item for item in results.values()
                          if item.get("status") == "needs_input"]
        missing = [item for item in state.outcomes if item.status == "needs_input"]
        if missing or lookup_missing:
            state.proposed_changes.clear()
            if state.reply_delivery.posts:
                return ""
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
                       ActionClass.CHANGE, ActionClass.IRREVERSIBLE)
                   and not results[step["id"]].get("delivered_as_reply")]
        if any(step["id"] not in prepared_steps
               or results[step["id"]].get("error") is not None
               or results[step["id"]].get("status") == "no_change"
               or results[step["id"]].get("flags", {}).get("status") in ("failed", "refused")
               for step in changes):
            state.proposed_changes.clear()
            return "" if state.reply_delivery.posts else None
        if state.proposed_changes:
            await prepare_preview(self.context)
            response = preview_text(state.proposed_changes)
            if state.reply_delivery.posts:
                await require_evidence_access(self.context)
                return response
            # Lookups that only resolve change targets are shown by the preview itself.
            steps_by_id = self.steps_by_id
            feeds_change: set[str] = set()
            pending = [owner for action in state.proposed_changes
                       for owner in step_dependencies(steps_by_id[action.step_id])]
            while pending:
                step_id = pending.pop()
                if step_id in feeds_change or step_id not in steps_by_id:
                    continue
                feeds_change.add(step_id)
                pending.extend(step_dependencies(steps_by_id[step_id]))
            answered = [steps_by_id[step_id] for step_id in self.read_results
                        if step_id not in feeds_change]
            reads = bool(answered)
            if reads or state.outcomes:
                state.preview_reply = response
                answer_ids = {step["id"] for step in answered}
                if state.outcomes:
                    answer_ids.update(step["id"] for step in self.plan["steps"]
                                      if self.registry[step["capability"]].action_class is ActionClass.OUTPUT)
                order = list(steps_by_id)
                state.preview_first = min(order.index(action.step_id) for action in state.proposed_changes) < min(
                    (order.index(step_id) for step_id in answer_ids), default=len(order),
                )
            answer = await self._feedback_reply(
                {step["id"]: self.read_results[step["id"]] for step in answered},
                RESULT_ANSWER_INSTRUCTION + " Changes are waiting for the member's "
                "confirmation; do not describe them as done.",
            ) if reads else ""
            if state.outcomes:
                if reads:
                    self.append_commands = True
                else:
                    answer = command_reply(state.outcomes)
            await require_evidence_access(self.context)
            if answer:
                return answer
            return response
        if state.reply_delivery.posts:
            await require_evidence_access(self.context)
            return ""
        if state.outcomes:
            await require_evidence_access(self.context)
            if not self.read_results:
                return command_reply(state.outcomes)
            # Commands keep their own outcome text. Independent read results still
            # need an answer, including reads from an earlier plan revision.
            self.append_commands = True
            state.has_read_answer = True
            self._model_results(self.read_results)
        try:
            if strict_mode(state.request_text) is not None:
                self._model_results(self.read_results)
                await require_evidence_access(self.context)
                return narrow_answer(state.request_text, state.grounding_evidence)
        except GroundingError as error:
            if error.kind == "source":
                error.kind = "list"
            return partial_answer("", state.grounding_evidence, error)
        if self.rounder.lookup_route:
            self._model_results(self.read_results)
            try:
                # The same validated formatter runs at final delivery. Reuse it
                # here when it already has the answer instead of buying synthesis.
                return narrow_answer(state.request_text, state.grounding_evidence)
            except GroundingError:
                # Missing or ambiguous evidence may still need another read.
                pass
        return None

    async def _feedback_reply(self, results, instruction, **details) -> str:
        pending = (AgentToolResult(
            self.decision.rounds[-1].tool_calls[0].call_id,
            json.dumps({"results": self._model_results(results), "instruction": instruction, **details},
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
                        "results": self._model_results(results),
                        "instruction": RESULT_ANSWER_INSTRUCTION + (
                            " Answer the reads only; command outcomes are delivered separately."
                            if self.append_commands else ""
                        ),
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
        check = check_plan(next_plan, self.registry, completed_steps=self.runner.completed_steps)
        if check.ok and (issue := social_reply_issue(next_plan, model_step.content)):
            check = replace(check, ok=False, error=issue)
        if self.context.operator_debug is not None:
            self.context.operator_debug.checked(model_step.tool_calls[0].call_id, check)
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
            check = check_plan(
                next_plan, self.registry, completed_steps=self.runner.completed_steps,
            )
            if check.ok and (issue := social_reply_issue(next_plan, corrected.content)):
                check = replace(check, ok=False, error=issue)
            if self.context.operator_debug is not None:
                self.context.operator_debug.checked(corrected.tool_calls[0].call_id, check)
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
        if any(
            step["capability"] + json.dumps(step["arguments"], sort_keys=True, default=str) in self.failed_reads
            for step in next_plan["steps"]
        ):
            raise AgentLimitReached(AGENT_PLAN_UNFINISHED)
        self.revisions += 1
        if next_plan.get("reply"):
            model_step = replace(model_step, content=next_plan["reply"])
            if not next_plan["steps"]:
                return next_plan["reply"]
        self.plan = next_plan
        self._reserve_answer()
        self.decision = type(self.decision)(None, self.plan, (model_step,))
        return None
