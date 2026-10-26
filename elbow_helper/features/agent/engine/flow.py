"""Request planning and final reply selection."""

from __future__ import annotations

import logging

from dataclasses import asdict, replace
from collections.abc import Mapping
import json
import time
from typing import Any
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.infrastructure.ai import AgentToolResult
from elbow_helper.infrastructure.ai import TextGenerationError
from ..actions.contracts import ActionClass
from ..access import (
    leadership_asker,
    require_access, require_evidence_access,
)
from ..plan.checker import check_plan, step_dependencies
from ..plan.format import PLAN_TOOL_NAME, REPLY_CAPABILITIES, answer_instructions, plan_definition
from ..plan.planning import PlanNotSettled, read_request, request_plan, reuse_reply_target, social_reply_issue
from .routing import SOCIAL_CAPABILITIES
from .social_reply import read_social_reply
from .social_counts import correct_social_counts
from ..plan.results import plan_feedback
from ..knowledge.answers import scoped_answer, scope_reply_issue, MEMBER_REPLY_INSTRUCTION
from ..actions.outcomes import command_reply
from ..actions.preview import is_preview_reply, preview_text
from ..actions.details import prepare_preview
from ..research.member_provenance import member_target_issue
from ..wording import (
    AGENT_ANSWER_UNFINISHED,
    AGENT_PLAN_UNFINISHED,
    AGENT_RESEARCH_UNFINISHED,
    ACTION_NOT_PREPARED,
    ACTION_UNAVAILABLE,
)
from . import budgets as limits
from .rounds import AgentGracefulEnd, AgentLimitReached, ModelRounds
from .steps import PlanRunner
from .grounding import (
    GROUNDING_INSTRUCTION, GroundingError, ground_answer, partial_answer,
)
from .factual_answers import numeric_conflicts
from .change_answers import unprepared_change_issue
from .role_claims import role_conflicts
from .unfinished import unfinished_answer
from .named_replies import named_reply
from .evidence_delta import EvidenceDelta, access_refused
from .answer_inputs import answer_evidence, read_handles, bounded_answer_evidence, serialized_answer_evidence
from .cohort_evidence import complete_activity_reads, cohort_writer_evidence, cohort_review_evidence, restorable_reference
from .assessment import DECISION_ANSWER_INSTRUCTION, assessment_limits, decision_answer, discussion_ids, read_decision_discussions
from .account_lists import account_index
from ..research.directory import scoped_channel_directory
from ..files.delivery import delivery_manifest
from ..conversation.context import estimate_tokens
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..identity import guild_identity
from .budgets import ContextBudget
from elbow_helper.infrastructure.ai.agent import AgentSession
from ..prompts import (
    RESULT_ANSWER_INSTRUCTION,
    EVENT_CONTEXT_INSTRUCTION,
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
        final_session_factory=None,
        answer_context="",
        answer_images=(),
    ) -> None:
        self.context = context
        self.session = session
        self.registry = registry
        self.budget = budget
        self.rounder = rounder
        self.runner = runner
        self.request_id = request_id
        self.final_session_factory = final_session_factory
        self.answer_context = answer_context
        self.answer_images = tuple(answer_images)
        self.completed_results = {}
        self.result_views = {}
        self.read_results = {}
        self.steps_by_id = {}
        self.append_commands = False
        self.failed_reads = set()
        self.failed_outputs = set()
        self.new_evidence = True
        self.scope_repair_attempted = False
        self.evidence_delta = EvidenceDelta()
        self.assessment_requested = False
        self.plan_limit_logged = False
        self.discussion_message_ids = set()
        self.decision_composed = False
        self.answer_session_inputs = None
        self.answer_accounts = {}

    def model_results(self, results):
        visible = {key: self.result_views[key] for key in results if key in self.result_views}
        self.discussion_message_ids.update(discussion_ids(visible))
        self.context.state.grounding_evidence.update(visible)
        return visible

    def _direct_capabilities(self):
        return SOCIAL_CAPABILITIES.intersection(self.registry) if self.rounder.social_route else frozenset()

    async def run(self) -> str:
        try:
            answer = await self._run()
        except AgentLimitReached as error:
            answer = await self._limit_reply(error)
        except PlanNotSettled as error:
            LOGGER.info("Agent planning recovery: request=%s reason=%s", self.request_id, error)
            answer = await self._limit_reply(AgentLimitReached(AGENT_PLAN_UNFINISHED))
        if self.assessment_requested and not self.rounder.final_attempted:
            # A tool-enabled follow-up can finish in prose instead of another
            # read plan. It is still a draft, not the assessed deliverable.
            final = await self._compact_reply()
            if final is not None:
                answer = final
        if self.assessment_requested:
            try:
                answer = decision_answer(answer, self.discussion_message_ids, accounts=self.answer_accounts)
            except ValueError as error:
                repaired = await self._repair_decision_format(answer, error)
                try:
                    answer = decision_answer(repaired.content, self.discussion_message_ids,
                                             accounts=self.answer_accounts)
                except ValueError as invalid:
                    raise TextGenerationError('Decision response could not be validated.') from invalid
        self.decision_composed = self.assessment_requested
        answer = await self._checked_scope_reply(answer)
        answer = await self._grounded_reply(answer)
        answer = await self._checked_changes(answer)
        answer = scoped_answer(self.context, answer)
        if self.append_commands and not self.context.state.reply_delivery.posts:
            answer = "\n\n".join(filter(None, (answer, command_reply(self.context.state.outcomes, has_findings=bool(answer.strip())))))
        # Previews are rendered from checked actions, not generated reply text.
        if not is_preview_reply(self.context, answer) and (issue := member_target_issue(self.context, answer)):
            # A late answer may follow reads, command feedback or grounding repair.
            # Give the model one chance to ask for the missing identity itself.
            repaired = await self._advance_answer(
                self._repair_results(), allow_tools=False,
                max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
                continuation_instruction=issue,
            )
            if (repaired.tool_calls or not repaired.content
                    or member_target_issue(self.context, repaired.content)):
                raise AgentGracefulEnd("Which member do you mean? Please mention them.")
            answer = scoped_answer(self.context, await self._grounded_reply(repaired.content))
            if member_target_issue(self.context, answer):
                raise AgentGracefulEnd("Which member do you mean? Please mention them.")
            answer = await self._checked_changes(answer)
        answer = await self._checked_scope_reply(answer)
        if getattr(self.rounder, "social_route", False):
            answer = correct_social_counts(answer, self.context.state.request_text)
        return named_reply(answer, self.context)

    async def _checked_scope_reply(self, answer):
        issue = scope_reply_issue(self.context, answer)
        if not issue:
            return scoped_answer(self.context, answer)
        if not self.scope_repair_attempted:
            self.scope_repair_attempted = True
            try:
                repaired = await self._advance_answer(
                    self._repair_results(), allow_tools=False,
                    max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
                    continuation_instruction=issue,
                )
                await require_evidence_access(self.context)
                if repaired.content and not repaired.tool_calls and not scope_reply_issue(self.context, repaired.content):
                    answer = scoped_answer(self.context, repaired.content)
                    answer = await self._grounded_reply(answer)
                    answer = await self._checked_changes(answer)
                    if not scope_reply_issue(self.context, answer) and not member_target_issue(self.context, answer):
                        return scoped_answer(self.context, answer)
            except (AgentGracefulEnd, TextGenerationError, TimeoutError) as error:
                LOGGER.info("Agent scope reply repair unavailable: request=%s reason=%s",
                            self.request_id, type(error).__name__)
        await require_evidence_access(self.context)
        compact = await self._compact_reply()
        if compact and not scope_reply_issue(self.context, compact):
            return scoped_answer(self.context, compact)
        # Retrieval narration is a presentation defect, not an access refusal.
        # Exhausted repairs must not erase an already completed factual answer.
        # Withholding claims still require the ordinary scope recovery below.
        if (issue == MEMBER_REPLY_INSTRUCTION
                and not scope_reply_issue(self.context, answer, check_narration=False)):
            return scoped_answer(self.context, answer)
        return unfinished_answer(self.context.state.request_text, self.model_results(self.completed_results))

    def _repair_results(self):
        """Close pending plan calls before any late answer-only repair."""
        return tuple(AgentToolResult(call.call_id, json.dumps({
            "results": self.model_results(self.completed_results),
        }, ensure_ascii=False, default=str))
            for call in getattr(self.rounder, "pending_calls", ()))

    async def _checked_changes(self, answer):
        issue = unprepared_change_issue(answer, self.context.state.proposed_changes)
        if not issue:
            return answer
        try:
            repaired = await self._advance_answer(
                self._repair_results(), allow_tools=False,
                max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
                continuation_instruction=issue,
            )
            await require_evidence_access(self.context)
            if repaired.content and not repaired.tool_calls:
                repaired_answer = await self._grounded_reply(repaired.content)
                if (not unprepared_change_issue(repaired_answer, self.context.state.proposed_changes)
                        and not member_target_issue(self.context, repaired_answer)):
                    return repaired_answer
        except (AgentGracefulEnd, TextGenerationError, TimeoutError) as error:
            LOGGER.info("Agent change reply repair unavailable: request=%s reason=%s",
                        self.request_id, type(error).__name__)
        await require_evidence_access(self.context)
        return ACTION_NOT_PREPARED

    async def _grounded_reply(self, answer):
        """All final paths, including direct, recovery and limit replies, pass here."""
        await require_evidence_access(self.context)
        if not answer:
            return answer
        answer = await self._checked_figures(answer)
        try:
            return ground_answer(answer, self.context.state.grounding_evidence)
        except GroundingError as error:
            failure = error
            LOGGER.info("Agent answer source issue: request=%s kind=%s reason=%s",
                        self.request_id, error.kind, error)
            # A rewrite cannot manufacture missing or ambiguous typed data.
            if error.kind != "source":
                return partial_answer(answer, self.context.state.grounding_evidence, error)
        try:
            repaired = await self._advance_answer(
                self._repair_results(), allow_tools=False,
                max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
                continuation_instruction=GROUNDING_INSTRUCTION +
                f" A source reference could not be resolved ({failure}). "
                "Correct the reference using evidence already read, or drop only that citation. "
                "Add a short uncertainty qualifier only to claims that depended on it. "
                "Preserve the rest of the useful answer "
                "and its requested format. Do not invent a source or withdraw supported recommendations.",
            )
            await require_evidence_access(self.context)
            if repaired.content and not repaired.tool_calls:
                # Citation repair must not introduce a contradictory figure, and
                # must not start another numeric repair round.
                answer = self._append_verified_figures(repaired.content)
                return ground_answer(answer, self.context.state.grounding_evidence)
        except GroundingError as error:
            failure = error
            LOGGER.info("Agent source repair incomplete: request=%s kind=%s reason=%s",
                        self.request_id, error.kind, error)
        except (AgentGracefulEnd, AgentLimitReached, TextGenerationError, TimeoutError) as error:
            LOGGER.info("Agent source repair unavailable: request=%s reason=%s",
                        self.request_id, type(error).__name__)
        await require_evidence_access(self.context)
        return partial_answer(answer, self.context.state.grounding_evidence, failure)

    def _limit_plan(self, plan):
        """Apply member limits before checking either an initial or revised plan."""
        if not isinstance(plan, dict) or leadership_asker(self.context):
            return plan
        if "assessment" not in plan and plan.get("effort") != "max":
            return plan
        plan = dict(plan)
        plan.pop("assessment", None)
        if plan.get("effort") == "max":
            plan["effort"] = "high"
        if not self.plan_limit_logged:
            LOGGER.info("Agent plan limited to member effort: request=%s", self.request_id)
            self.plan_limit_logged = True
        return plan

    def _planning_effort(self):
        requested = AgentReasoningEffort(getattr(self, "plan", {}).get("effort", "low"))
        floor = getattr(self.rounder, "request_effort", AgentReasoningEffort.HIGH)
        effort = max((requested, floor), key=list(AgentReasoningEffort).index)
        return (effort if leadership_asker(self.context)
                else min((effort, AgentReasoningEffort.HIGH), key=list(AgentReasoningEffort).index))

    async def _advance_answer(self, results=(), **options):
        """Keep lookups cheap and give assessments the final reasoning allowance.

        A large evidence set must leave room for visible text. Inheriting a
        maximum-effort plan can spend the entire final allowance on reasoning.
        Repairs and backends without isolated sessions need the same boundary.
        ModelRounds still owns every token, call, deadline and access check.
        """
        planning_effort = getattr(self.rounder, "request_effort", AgentReasoningEffort.HIGH)
        composition_effort = (AgentReasoningEffort.MAX if self.assessment_requested
                              and not options.get('allow_tools', True) and not self.rounder.final_attempted
                              else AgentReasoningEffort.HIGH if self.assessment_requested
                              and options.get('allow_tools', True)
                              else AgentReasoningEffort.NONE if self.assessment_requested
                              else AgentReasoningEffort.LOW)
        if self.assessment_requested:
            if not options.get('allow_tools', True) and not self.decision_composed:
                options['continuation_instruction'] = '\n'.join(filter(None, (
                    options.get('continuation_instruction'), DECISION_ANSWER_INSTRUCTION)))
            elif self.decision_composed:
                options['continuation_instruction'] = '\n'.join(filter(None, (
                    options.get('continuation_instruction'),
                    'The decision has already been composed. Return corrected plain reply text for this repair.')))
            if not options.get('allow_tools', True) and not self.rounder.final_attempted:
                options['final'] = True
                policy = self.budget.request_limits
                if policy and policy.final_output_tokens:
                    options['max_output_tokens'] = policy.final_output_tokens
        if self.context.state.attachments:
            options["continuation_instruction"] = "\n".join(filter(None, (
                options.get("continuation_instruction"),
                "Delivery for this reply: " + json.dumps(
                    delivery_manifest(self.context.state.attachments), ensure_ascii=False),
                "Describe these prepared outputs plainly. Read pagination limits do not limit the files. "
                "Do not discuss evidence readability or internal delivery mechanics.",
            )))
        self.rounder.request_effort = composition_effort
        try:
            return await self.rounder.advance(results, **options, reasoning_effort=composition_effort)
        finally:
            self.rounder.request_effort = planning_effort

    async def _repair_decision_format(self, answer, error):
        """Repair the response shape without resending the full research context."""
        instruction = ('Correct the invalid decision response: ' + str(error)
            + '\nPreserve the draft facts and source links; add no facts or advice. '
              'An unavailable discussion does not establish a decision. '
              'Keep readable facts and ask about the unresolved choice.')
        if self.final_session_factory is not None:
            prompt = json.dumps({'request': self.context.state.request_text, 'draft': answer,
                'verified_discussion_message_ids': sorted(self.discussion_message_ids)}, ensure_ascii=False)
            projected = estimate_tokens(DECISION_ANSWER_INSTRUCTION + prompt + instruction) + 512
            if self.budget.request_output_limit(projected, allow_tools=False) > 0:
                await require_evidence_access(self.context)
                session = self.final_session_factory(system_prompt=DECISION_ANSWER_INSTRUCTION,
                    prompt=prompt, tools=(), max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS)
                if session is not None and session is not self.rounder.session:
                    self.rounder.session = session
                    self.rounder.pending_calls = self.rounder.pending_results = ()
                    self.rounder.unpublished = None
                    self.budget.next_input_estimate = projected
        return await self._advance_answer((), allow_tools=False,
            continuation_instruction=instruction,
            max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS)

    def _append_verified_figures(self, answer):
        if self.context.state.family_catalogue:
            return answer
        conflicts = numeric_conflicts(self.context.state.request_text, answer,
                                      self.context.state.grounding_evidence)
        roles = role_conflicts(answer, self.context.state.grounding_evidence)
        if roles:
            return "\n\n".join((answer, "Verified facts: " + " ".join((*conflicts, *roles))))
        return "\n\n".join((answer, "Verified figures: " + " ".join(conflicts))) if conflicts else answer

    async def _checked_figures(self, answer):
        if self.context.state.family_catalogue:
            return answer
        conflicts = numeric_conflicts(self.context.state.request_text, answer,
                                      self.context.state.grounding_evidence)
        conflicts += role_conflicts(answer, self.context.state.grounding_evidence)
        if not conflicts:
            return answer
        try:
            repaired = await self._advance_answer(
                self._repair_results(), allow_tools=False,
                max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
                continuation_instruction=(
                    "Correct figures and in-game roles that contradict these typed tool values "
                    "for the same subject and field. Preserve useful prose, explanations and requested format. "
                    "Do not confuse a clan's total membership with a filtered group.\n"
                    + "\n".join(conflicts)
                ),
            )
            await require_evidence_access(self.context)
            if repaired.content and not repaired.tool_calls:
                answer = repaired.content
        except (AgentGracefulEnd, TextGenerationError, TimeoutError) as error:
            LOGGER.info("Agent figure repair unavailable: request=%s reason=%s",
                        self.request_id, type(error).__name__)
        await require_evidence_access(self.context)
        return self._append_verified_figures(answer)

    async def _limit_reply(self, error: AgentLimitReached) -> str:
        if self.context.state.reply_delivery.posts:
            return ""
        await require_evidence_access(self.context)
        if self.rounder.social_route or self.rounder.game_advice_route:
            # No completed social reply exists after a failed bounded rewrite.
            # Surface an operational failure, never a draft or canned answer.
            self.context.state.terminal_cause = 'limit'
            raise TextGenerationError("Short reply could not be completed within request limits.") from error
        evidence = self.model_results(self.completed_results)
        def fallback():
            answer = unfinished_answer(self.context.state.request_text, evidence)
            partial = self.rounder.partial_content
            if partial and not partial.lstrip().startswith(("{", "```")):
                return partial.rstrip() if evidence else partial.rstrip() + "\n\n" + answer
            return answer
        # A final-only session avoids resending the large capability catalogue
        # and accumulated plans. It uses the same cumulative budget and access
        # checks; this is not an extra allowance after exhaustion.
        if self.final_session_factory is None or self.rounder.final_attempted:
            return fallback()
        policy = self.budget.request_limits
        if policy and (self.budget.spent_rounds >= policy.rounds
                       or self.budget.spent_output >= policy.output_tokens):
            return fallback()
        try:
            answer = await self._compact_reply()
            if answer is not None:
                return answer
            if self.rounder.final_attempted:
                return fallback()
            # A backend without fresh sessions can still finish in its current
            # conversation. Close pending calls and retain the same shared caps.
            pending = self.rounder.pending_results or tuple(
                AgentToolResult(call.call_id, json.dumps({"results": evidence},
                                                        ensure_ascii=False, default=str))
                for call in self.rounder.pending_calls)
            projected = self.budget.projected_input(pending) + estimate_tokens(LIMIT_ANSWER_INSTRUCTION)
            if (self.budget.context_window_tokens is not None
                    and projected >= self.budget.context_window_tokens):
                return fallback()
            reply = await self._advance_answer(
                pending, allow_tools=False,
                max_output_tokens=min(limits.FINAL_MAX_OUTPUT_TOKENS,
                                      policy.round_output_tokens if policy else limits.FINAL_MAX_OUTPUT_TOKENS),
                continuation_instruction=LIMIT_ANSWER_INSTRUCTION, final=True,
            )
            return reply.content if reply.content and not reply.tool_calls else fallback()
        except (AgentGracefulEnd, TextGenerationError, TimeoutError):
            return fallback()

    def _answer_system(self, *, allow_more):
        system = answer_instructions(self.registry if allow_more else None)
        if self.assessment_requested:
            if allow_more:
                system += ('\nThis round checks whether more evidence is needed. Submit a plan only for '
                    'missing necessary evidence. When ready, return only {"research_complete":true}. '
                    'Do not draft the assessment here; a separate final composition follows. '
                    'This review contains checked summaries, retained-table metadata and discussion pages. '
                    'The final writer receives every authorized retained row. Use read_handles for missing facts; '
                    'do not reread a complete cohort just to reproduce its rows. Read omitted details when '
                    'their meaning is necessary for the remaining research.')
            else:
                system += ('\n\nFor assessments, use the decision JSON schema supplied with the round instructions. '
                    'It replaces the normal plain reply and private-answer envelope formats. '
                    'After the decision has been composed, later repair instructions may request plain reply text.')
        return system

    def _answer_frame(self, evidence, *, allow_more, instruction):
        return json.dumps({"request": self.context.state.request_text,
            "context": self.answer_context,
            "delivery": delivery_manifest(self.context.state.attachments),
            "composition_constraints": instruction,
            **({"read_handles": read_handles(evidence)} if allow_more else {}),
            "images": [image.label for image in self.answer_images]
            + self.context.state.image_source_status}, ensure_ascii=False)

    async def _prepare_answer_session(self, *, allow_more=False, instruction=""):
        """Keep research pages small; restore full authorized tables for composition."""
        if self.final_session_factory is None or self.rounder.final_attempted:
            return False
        await require_evidence_access(self.context)
        policy = self.budget.request_limits
        review_only = self.assessment_requested and allow_more
        system = self._answer_system(allow_more=allow_more)
        tools = (plan_definition(self.registry),) if allow_more else ()
        if not review_only:
            await complete_activity_reads(self)
        evidence = self.model_results(self.completed_results)
        transport = {**self.context.state.grounding_evidence, **evidence}
        prompt = self._answer_frame(evidence, allow_more=allow_more, instruction=instruction)
        room = (policy.input_tokens - self.budget.spent_input if policy else 32_000)
        room -= estimate_tokens(system + prompt) + 1024
        if not allow_more:
            room -= estimate_tokens(LIMIT_ANSWER_INSTRUCTION)
        if self.assessment_requested and not self.decision_composed and not review_only:
            room -= estimate_tokens(DECISION_ANSWER_INSTRUCTION)
        if room <= 0:
            return False
        # Local/replied messages are independent evidence, including on a late repair.
        evidence = await cohort_writer_evidence(self.context,
            transport, snapshots=self.runner.report_snapshots)
        review_evidence = cohort_review_evidence(transport, evidence) if review_only else None
        self.discussion_message_ids.update(discussion_ids(evidence))
        self.context.state.grounding_evidence.update(evidence)
        selection_limits = assessment_limits(evidence) if self.assessment_requested else {}
        if self.assessment_requested and not review_only:
            # Listed accounts are written from these authorized rows, not model copies.
            self.answer_accounts = account_index(evidence, selection_limits) or self.answer_accounts
        subjects = {identity: step.get('arguments', {}) for identity, step in self.steps_by_id.items()}
        evidence = answer_evidence(evidence, guild=self.context.guild, subjects=subjects)
        supplementary = {}
        if self.context.application_owner is not None:
            supplementary['bot_identity'] = {'owner_and_operator': asdict(guild_identity(
                self.context.application_owner, self.context.guild))}
        # Planning context is deliberately reduced for synthesis. Preserve the
        # current authorized directory as factual evidence in that reduced view.
        directory = json.loads(await scoped_channel_directory(self.context))
        if directory:
            supplementary['channel_directory'] = {'channels': directory}
        if selection_limits:
            supplementary['assessment_limits'] = selection_limits
        evidence.update(supplementary)
        if self.assessment_requested:
            # Reserve the actual complete writer input, not another copy of
            # the small review page. This measurement makes no provider call.
            final_prompt = self._answer_frame({}, allow_more=False, instruction=instruction)
            final_prompt += '\nAvailable evidence:\n' + serialized_answer_evidence(evidence)
            final_system = self._answer_system(allow_more=False)
            self.budget.final_input_estimate = (
                estimate_tokens(final_system + final_prompt) + estimate_tokens('[]') + 512
                + estimate_tokens(LIMIT_ANSWER_INSTRUCTION)
                + estimate_tokens(LIMIT_ANSWER_INSTRUCTION + '\n' + DECISION_ANSWER_INSTRUCTION))
        if review_only:
            evidence = answer_evidence(review_evidence, guild=self.context.guild, subjects=subjects)
            evidence.update(supplementary)
        complete_measures = any(result.get('family') in (
            'read_cwl_results', 'read_war_attendance', 'read_ranked_activity', 'read_clan_rosters',
            'read_member_cohort', 'read_member_accounts') or restorable_reference(result)
            for result in self.completed_results.values())
        bounded = bounded_answer_evidence(evidence, room,
            None if complete_measures and not review_only else self.runner.evidence_limit)
        prompt += "\nAvailable evidence:\n" + bounded
        projected = (estimate_tokens(system + prompt) + estimate_tokens(json.dumps(
            [tool.parameters for tool in tools], ensure_ascii=False)) + 512
            + estimate_tokens(LIMIT_ANSWER_INSTRUCTION if not allow_more else ""))
        if self.budget.request_output_limit(projected, allow_tools=allow_more, final=not allow_more) <= 0:
            return False
        session = self.final_session_factory(system_prompt=system, prompt=prompt, tools=tools,
                                             max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
                                             **({"images": self.answer_images} if self.answer_images else {}))
        if session is None or session is self.rounder.session:
            return False
        capacity = getattr(session, "context_window_tokens", None)
        if capacity is not None and projected + limits.MIN_FINAL_OUTPUT_TOKENS > capacity:
            return False
        self.rounder.session = session
        self.rounder.pending_calls = self.rounder.pending_results = ()
        # Evidence is now in the new session's initial prompt. Unlike pending
        # tool results, that prompt cannot be replaced with an access refusal.
        # A last-moment revocation must abort before the provider is called.
        self.rounder.unpublished = None
        self.budget.next_input_estimate = projected
        if not allow_more:
            self.answer_session_inputs = (system, prompt, projected)
            self.rounder.partial_content = ''
        return True

    async def _fresh_answer_recovery(self):
        """Reuse the final evidence prefix without resending failed reasoning."""
        if self.final_session_factory is None or self.answer_session_inputs is None:
            return
        await require_evidence_access(self.context)
        system, prompt, projected = self.answer_session_inputs
        session = self.final_session_factory(system_prompt=system, prompt=prompt, tools=(),
            max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
            **({'images': self.answer_images} if self.answer_images else {}))
        if session is not None and session is not self.rounder.session:
            self.rounder.session = session
            self.rounder.pending_calls = self.rounder.pending_results = ()
            self.rounder.unpublished = None
            self.budget.next_input_estimate = projected

    async def _compact_reply(self, *, instruction=""):
        """Synthesize once without resending plans or search indexes."""
        if not await self._prepare_answer_session(instruction=instruction):
            return None
        repaired = False
        try:
            try:
                reply = await self._advance_answer(
                    (), allow_tools=False,
                    max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
                    continuation_instruction=LIMIT_ANSWER_INSTRUCTION, final=True,
                )
            except TextGenerationError:
                repaired = True
                # A provider can still emit control markup with no tools
                # offered. One answer-only repair shares the remaining caps.
                await self._fresh_answer_recovery()
                reply = await self._advance_answer(
                    (), allow_tools=False,
                    max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
                    continuation_instruction=LIMIT_ANSWER_INSTRUCTION,
                )
            if reply.tool_calls and not repaired:
                refusals = tuple(AgentToolResult(call.call_id, json.dumps({
                    "flags": {"status": "refused", "reason": "answer_only"},
                })) for call in reply.tool_calls)
                reply = await self._advance_answer(
                    refusals, allow_tools=False,
                    max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
                    continuation_instruction=LIMIT_ANSWER_INSTRUCTION,
                )
            elif not reply.content.strip() and not repaired:
                # Empty visible content is not a finished answer. Use the same
                # reserved repair allowance as malformed provider output.
                await self._fresh_answer_recovery()
                reply = await self._advance_answer(
                    (), allow_tools=False,
                    max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
                    continuation_instruction=LIMIT_ANSWER_INSTRUCTION,
                )
        except AgentLimitReached:
            if not self.assessment_requested or not self.rounder.final_attempted:
                return None
            try:
                draft = self.rounder.partial_content
                await self._fresh_answer_recovery()
                reply = await self._advance_answer((), allow_tools=False,
                    max_output_tokens=limits.INITIAL_MAX_OUTPUT_TOKENS,
                    continuation_instruction='The previous response stopped before completing the answer. '
                        'Return one complete concise answer from the supplied results, not a continuation '
                        'or fragment. Keep every requested item. Do not repeat the deliberation.'
                        + ('\nComplete the interrupted assessment below. Preserve its intent and constraints '
                           'where supported by the supplied evidence; verify its claims rather than treating '
                           'generated text as a source. Return the full required response, including all '
                           'requested rows. Incomplete assessed draft:\n' + json.dumps(draft, ensure_ascii=False)
                           if draft else ''))
            except (AgentGracefulEnd, TextGenerationError, TimeoutError):
                return None
        except (AgentGracefulEnd, TextGenerationError, TimeoutError):
            return None
        if reply.tool_calls or not reply.content:
            return None
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
        if getattr(self.rounder, 'game_advice_route', False):
            reply = await self.rounder.advance(allow_tools=False,
                reasoning_effort=AgentReasoningEffort.LOW, max_output_tokens=1024)
            return reply.content.strip() if not reply.tool_calls else ''
        initial_step = None
        if self.rounder.social_route:
            answer, initial_step = await read_social_reply(self)
            if initial_step is None:
                return answer
        self.decision = await read_request(
            self.session,
            self.registry,
            self.request_id,
            advance=self.rounder.advance,
            on_check=(self.context.operator_debug.checked
                      if self.context.operator_debug is not None else None),
            reasoning_effort=(AgentReasoningEffort.LOW if self.rounder.social_route
                              else AgentReasoningEffort.HIGH),
            validate_answer=lambda answer, plan: unprepared_change_issue(
                answer, self.context.state.proposed_changes,
            ) or member_target_issue(
                self.context, answer, self_target=(plan or {}).get("self_target", False),
            ),
            direct_capabilities=self._direct_capabilities,
            initial_step=initial_step,
            limit_plan=self._limit_plan,
        )
        self.context.state.member_self_target = self.decision.self_target
        if self.decision.answer is not None:
            await require_evidence_access(self.context)
            return self.decision.answer
        self.plan = self.decision.plan
        self.revisions = 0
        self._reserve_answer()
        while self.rounder.rounds < limits.MAX_MODEL_ROUNDS:
            self.assessment_requested = leadership_asker(self.context) and (
                self.assessment_requested or self.plan.get('assessment', False))
            if self.assessment_requested and self.budget.request_limits is not None:
                self.budget.request_limits = limits.ASSESSMENT_LIMITS
            discussion_results = await read_decision_discussions(self)
            previous_calls = self.runner.tool_calls
            results = await self.runner.run(self.plan)
            self.new_evidence = self.runner.tool_calls > previous_calls or any(
                result.get("cause") in ("invalid_arguments", "sql_schema_error")
                for result in results.values()
            )
            self.steps_by_id.update({step["id"]: step for step in self.plan["steps"]})
            self.completed_results.update(results)
            self.failed_reads.update(
                step["capability"] + json.dumps(step["arguments"], sort_keys=True, default=str)
                for step in self.plan["steps"] if results.get(step["id"], {}).get("cause")
                in ("invalid_arguments", "sql_schema_error")
            )
            self.failed_outputs.update(
                step["capability"] for step in self.plan["steps"]
                if self.registry[step["capability"]].effect is AgentCapabilityEffect.ARTIFACT
                and results.get(step["id"], {}).get("error") == ACTION_UNAVAILABLE
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
            if (social_steps and all(step["capability"] in REPLY_CAPABILITIES for step in social_steps)
                    and all("error" not in results.get(step["id"], {})
                            for step in social_steps if step["capability"] == "set_discord_reply_target")
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
                self.read_results if self.append_commands else {**discussion_results, **results},
            )
            if response is not None:
                return response
            response = await self._revise(model_step)
            if response is not None:
                return response
        raise AgentLimitReached(AGENT_RESEARCH_UNFINISHED)

    def _reserve_answer(self) -> None:
        reserve = limits.FINAL_MAX_OUTPUT_TOKENS
        if self.budget.request_limits is not None:
            reserve = min(reserve, self.budget.request_limits.round_output_tokens)
        self.budget.final_answer_reserve = reserve

    async def _result_response(self, results: Mapping[str, Any]) -> str | None:
        state = self.context.state
        self.append_commands = False
        if len(results) == 1:
            replay = next(iter(results.values()))
            if (replay.get("family") == "read_conversation_evidence" and replay.get("mode") == "replay"
                    and isinstance(replay.get("delivered_answer"), str)):
                return replay["delivered_answer"]
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
            self.model_results(self.read_results)
        return None

    async def _feedback_reply(self, results, instruction, **details) -> str:
        pending = (AgentToolResult(
            self.decision.rounds[-1].tool_calls[0].call_id,
            json.dumps({"results": self.model_results(results), "instruction": instruction, **details},
                       ensure_ascii=False, default=str),
        ),)
        if await self._prepare_answer_session(instruction=json.dumps(
                {"instruction": instruction, **details}, ensure_ascii=False, default=str)):
            pending = ()
        reply = await self._advance_answer(
            pending, allow_tools=False,
            max_output_tokens=limits.FINAL_MAX_OUTPUT_TOKENS,
        )
        if reply.tool_calls or not reply.content:
            raise PlanNotSettled("The feedback round did not supply an answer.")
        await require_evidence_access(self.context)
        return reply.content

    def _planned_reads_complete(self, results):
        """Finish a declared factual read once its selected evidence is complete."""
        if not self.plan.get('finish_after_reads') or not results:
            return False
        for value in results.values():
            if access_refused(value):
                # A refused source is terminal, not an incomplete page to
                # pursue again. The writer decides whether it was requested.
                continue
            if (value.get('error') or value.get('status') == 'needs_input'
                    or value.get('flags', {}).get('status') in ('failed', 'refused')
                    or value.get('selection_complete') is False or value.get('count_complete') is False
                    or value.get('missing_sources')
                    or value.get('source_next_cursor')
                    or isinstance(value.get('coverage'), dict) and value['coverage'].get('next_cursor')
                    or value.get('family') == 'read_community_reference' and value.get('mode') == 'index'):
                return False
            if value.get('next_offset') is not None or value.get('next_cursor') is not None:
                # These complete snapshots are restored and authorized again
                # at the writer boundary. Other source pages still need reads.
                restored = (value.get('family') in (
                    'read_war_attendance', 'read_cwl_results', 'read_ranked_activity',
                    'read_member_accounts', 'read_clan_rosters')
                    or value.get('family') == 'read_member_cohort'
                    and value.get('basis') in ('clan_rosters', 'role_holders', 'joined_discord')
                    or restorable_reference(value))
                if value.get('complete_snapshot') is not True or not restored:
                    return False
        return True

    async def _answer_round(self, results: Mapping[str, Any]) -> tuple[str | None, Any]:
        discussion_reads = any(
            self.steps_by_id.get(identity, {}).get('capability') in (
                'read_conversation_evidence', 'read_discord_channel_history', 'read_conversation_history')
            for identity in results
        )
        if discussion_reads and self.budget.request_limits == limits.FAMILY_LIMITS:
            # Discussion discovery needs semantic review between chunks. Use
            # the existing research caps instead of a simple factual read cap.
            self.budget.request_limits = limits.RESEARCH_LIMITS
        visible, changed = self.evidence_delta.prepare(self.model_results(results), {
            step_id: {field: self.steps_by_id.get(step_id, {}).get(field)
                      for field in ("capability", "arguments")} for step_id in results
        })
        self.new_evidence &= changed
        denied = any(access_refused(value) for value in results.values())
        terminal = (bool(results) and all(access_refused(value) for value in results.values())
                    and not any(step_id not in results and not value.get("error")
                                for step_id, value in self.completed_results.items()))
        instruction = RESULT_ANSWER_INSTRUCTION
        if discussion_reads:
            instruction += (
                " Judge whether the actual discussion answers every requested part. "
                "If incomplete, continue the same channel with source_next_arguments from read_handles "
                "before answering. Select messages by meaning, including differently worded proposals "
                "and their follow-ups. If the dated window starts too late, widen it. "
                "Unread image metadata cannot establish contents or explain a missing answer. "
                "If further permitted reads still leave a gap, give supported findings and ask "
                "one precise question locating the missing discussion."
            )
        if self.failed_outputs:
            instruction += (
                " File delivery failed for " + ", ".join(sorted(self.failed_outputs))
                + ". Do not retry those outputs in this request. Previously read rows remain "
                "available: answer from them and name only genuinely missing evidence. "
                "Mention a missing file only if requested, after the substantive answer, "
                "without tool mechanics."
            )
        if self.read_results:
            instruction += " " + EVENT_CONTEXT_INSTRUCTION
        if denied:
            instruction += (
                " An explicit access boundary was reached. Stop that restricted part; do not "
                "look for the same material through other queries, threads, channels or searches. "
                "Give permitted findings; mention a limit only if it blocks a requested finding, "
                "using real role names. Unavailable optional background does not require a caveat "
                "or a private-answer offer. "
                "Only independent permitted parts may need further reads."
            )
        pending = tuple(
            AgentToolResult(
                call.call_id,
                json.dumps(
                    {
                        "results": visible,
                        "instruction": instruction + (
                            " Answer the reads only; command outcomes are delivered separately."
                            if self.append_commands else ""
                        ),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            ) for call in self.decision.rounds[-1].tool_calls
        )
        projected = self.budget.projected_input(pending)
        answer_limit = limits.FINAL_MAX_OUTPUT_TOKENS
        available = (
            answer_limit
            if self.budget.context_window_tokens is None
            else min(answer_limit, self.budget.context_window_tokens - projected)
        )
        if available < limits.MIN_FINAL_OUTPUT_TOKENS:
            self.rounder.pending_results = pending
            raise AgentLimitReached(AGENT_RESEARCH_UNFINISHED)
        self.budget.output_reserve = available
        fresh_review = self.assessment_requested and self.final_session_factory is not None
        allow_more = (
            self.new_evidence
            and not terminal
            and not self._planned_reads_complete(results)
            and self.rounder.rounds < limits.MAX_MODEL_ROUNDS - 1
            and self.runner.tool_calls < limits.MAX_TOOL_CALLS
            and self.runner.evidence_characters < self.runner.evidence_limit
            and (fresh_review or (
                self.budget.request_can_research(projected)
                and self.budget.can_continue_tools(projected, result_reserve=8192)))
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
        if allow_more and await self._prepare_answer_session(allow_more=True, instruction=instruction):
            # Results are in this fresh session's prompt. This is the same
            # provider round that previously produced a draft or another plan.
            pending = ()
            allow_more = (self.budget.request_can_research(self.budget.next_input_estimate)
                and self.budget.can_continue_tools(self.budget.next_input_estimate, result_reserve=8192))
        elif fresh_review:
            allow_more = False
        if not allow_more:
            # Choose the final protocol before ModelRounds disables tools.
            # The preceding review admission uses its compact input and the
            # independently measured complete final/recovery input.
            compact = await self._compact_reply(instruction=instruction)
            if compact is not None:
                return compact, None
            if self.rounder.final_attempted:
                raise AgentLimitReached(AGENT_ANSWER_UNFINISHED)
        step = await self._advance_answer(
            pending,
            allow_tools=allow_more,
            max_output_tokens=available,
            continuation_instruction=LIMIT_ANSWER_INSTRUCTION if not allow_more else None,
        )
        if (not step.tool_calls and step.content
                and (issue := unprepared_change_issue(step.content, self.context.state.proposed_changes))
                and allow_more and self.rounder.last_allow_tools):
            # A resolved change target still needs an actual change plan. Keep
            # the existing research budget; answer-only paths repair in run().
            step = await self._advance_answer(
                (), allow_tools=True,
                max_output_tokens=available, continuation_instruction=issue,
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
            recovery = await self._advance_answer(
                refusals,
                allow_tools=False,
                max_output_tokens=available,
                continuation_instruction=LIMIT_ANSWER_INSTRUCTION,
            )
            if recovery.tool_calls or not recovery.content:
                raise AgentGracefulEnd(AGENT_RESEARCH_UNFINISHED)
            await require_evidence_access(self.context)
            return recovery.content, None
        return None, step

    async def _revise(self, model_step: Any) -> str | None:
        next_plan = reuse_reply_target(request_plan(model_step, self._direct_capabilities()),
                                      self.runner.completed_steps, reply_to=self.context.state.reply_to,
                                      ping_author=self.context.state.ping_author)
        next_plan = self._limit_plan(next_plan)
        if next_plan is None and (len(model_step.tool_calls) != 1 or model_step.tool_calls[0].name != PLAN_TOOL_NAME):
            raise PlanNotSettled("The revision did not supply a request plan.")
        check = check_plan(next_plan, self.registry, completed_steps=self.runner.completed_steps)
        if check.ok and (issue := social_reply_issue(next_plan, model_step.content)):
            check = replace(check, ok=False, error=issue)
        if self.context.operator_debug is not None:
            for call in model_step.tool_calls:
                self.context.operator_debug.checked(call.call_id, check)
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
                tuple(
                    AgentToolResult(
                        call.call_id,
                        json.dumps(
                            plan_feedback(check.error, step_id=check.step_id, offered=check.offered)
                        ),
                    ) for call in model_step.tool_calls
                ),
                reasoning_effort=self._planning_effort(),
            )
            if not corrected.tool_calls and corrected.content:
                return corrected.content
            next_plan = reuse_reply_target(request_plan(corrected, self._direct_capabilities()),
                                          self.runner.completed_steps, reply_to=self.context.state.reply_to,
                                          ping_author=self.context.state.ping_author)
            next_plan = self._limit_plan(next_plan)
            if next_plan is None and (len(corrected.tool_calls) != 1 or corrected.tool_calls[0].name != PLAN_TOOL_NAME):
                raise PlanNotSettled("The correction did not supply a request plan.")
            check = check_plan(
                next_plan, self.registry, completed_steps=self.runner.completed_steps,
            )
            if check.ok and (issue := social_reply_issue(next_plan, corrected.content)):
                check = replace(check, ok=False, error=issue)
            if self.context.operator_debug is not None:
                for call in corrected.tool_calls:
                    self.context.operator_debug.checked(call.call_id, check)
            LOGGER.info(
                "Agent plan correction: request=%s ok=%s plan=%s error=%s",
                self.request_id,
                check.ok,
                json.dumps(next_plan, ensure_ascii=False),
                check.error,
            )
            if not check.ok:
                raise PlanNotSettled(check.error)
            model_step = corrected
        if any(
            step["capability"] in self.failed_outputs
            or step["capability"] + json.dumps(step["arguments"], sort_keys=True, default=str) in self.failed_reads
            for step in next_plan["steps"]
        ):
            raise AgentLimitReached(AGENT_PLAN_UNFINISHED)
        self.revisions += 1
        self.context.state.member_self_target |= next_plan.get("self_target", False)
        if next_plan.get("reply"):
            model_step = replace(model_step, content=next_plan["reply"])
            if not next_plan["steps"]:
                return next_plan["reply"]
        self.plan = next_plan
        self._reserve_answer()
        self.decision = type(self.decision)(None, self.plan, (model_step,))
        return None
