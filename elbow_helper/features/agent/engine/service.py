"""Bounded orchestration for the agent."""

from __future__ import annotations

import logging
import json
from dataclasses import replace

import asyncio
from datetime import datetime, timezone
import time
from elbow_helper.infrastructure.ai import AgentModel
from elbow_helper.infrastructure.ai import TextGenerationError
from ..models import AgentRequestContext
from ..conversation.debug import OperatorDebugRecord
from ..conversation.replay import replay_answer
from ..actions.contracts import ActionClass
from ..access import (
    AgentAccessLost,
    require_evidence_access,
    KNOWN_ACCESS_REQUIREMENTS,
    has_access_requirements,
)
from ..conversation.context import compile_context, estimate_tokens
from ..discord_actions.reactions import server_reaction_context
from .registry import build_agent_tools
from .usage import RequestUsage
from .budgets import ContextBudget
from .routing import (
    simple_lookup, LOOKUP_CAPABILITIES, LOOKUP_INSTRUCTION, EXPAND_DEFINITION,
)
from ..reports.tools import filter_fields
from ..plan.format import plan_definition, system_instructions
from ..plan.planning import PlanNotSettled
from ..capabilities import enabled_adapters
from ..commands.bridge import build_command_tools
from ..wording import AGENT_PLAN_UNFINISHED
from . import budgets as limits
from .rounds import AgentGracefulEnd, ModelRounds
from .steps import PlanRunner
from .flow import AnswerFlow
from .tool_call import execute_tool

LOGGER = logging.getLogger(__name__)


class AgentUnavailableError(RuntimeError):
    """Raised when an agent request cannot produce a final answer."""


class AgentService:
    """Plan checked reads and answer from their results."""

    def __init__(self, model: AgentModel, *, data_guide=None) -> None:
        self._model = model
        self._data_guide = data_guide

    execute_tool = staticmethod(execute_tool)


    @staticmethod
    def _log_completion(
        *,
        context: AgentRequestContext,
        rounds: int,
        tool_calls: int,
        evidence_characters: int,
        usage: RequestUsage,
        status: str,
        elapsed_ms: int,
    ) -> None:
        LOGGER.info(
            (
                (
                    'Agent usage: request=%s status=%s '
                    'elapsed_ms=%s invoker=%s channel=%s '
                    'rounds=%s tools=%s evidence_chars=%s '
                    'prompt_tokens=%s completion_tokens=%s '
                    'cache_hit_tokens=%s cache_miss_tokens=%s '
                    'attempted_rounds=%s unknown_token_rounds=%s '
                    'unknown_cache_rounds=%s'
                )
            ),
            getattr(context.source_message, "id", None),
            status,
            elapsed_ms,
            context.member.id,
            context.source_message.channel.id,
            rounds,
            tool_calls,
            evidence_characters,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.cache_hit_tokens,
            usage.cache_miss_tokens,
            usage.attempted_rounds,
            usage.unknown_token_rounds,
            usage.unknown_cache_rounds,
        )

    def _start_flow(
        self,
        *,
        question: str,
        local_context: str,
        context: AgentRequestContext,
        conversation_history: str,
        images=(),
    ):
        context.state.grounding_evidence.clear()
        registry = build_agent_tools(getattr(context.bot, "gif_client", None))
        command_capabilities = {}
        actions_available = getattr(context.bot, "tree", None) is not None
        if not actions_available:
            registry = {
                name: tool
                for name, tool in registry.items()
                if tool.action_class not in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE)
            }
        if actions_available:
            command_tools, command_capabilities = build_command_tools(
                context.bot,
                enabled_adapters(),
            )
            registry.update(command_tools)
        full_registry = dict(registry)
        lookup = simple_lookup(question) and not images
        if lookup:
            registry = {name: tool for name, tool in registry.items()
                        if name in LOOKUP_CAPABILITIES}

        def research_instructions():
            levels = {
                level for level in KNOWN_ACCESS_REQUIREMENTS
                if has_access_requirements(context.guild, context.member.id, {level})
            }
            knowledge = context.knowledge_store.load().prompt(levels) if context.knowledge_store else None
            if knowledge:
                context.state.required_access.update(knowledge.required_access)
            return system_instructions(
                full_registry, actions_enabled=actions_available,
                data_guide=self._data_guide.for_levels(levels) if self._data_guide else "",
                community_knowledge=knowledge.text if knowledge else "",
            )

        definition = plan_definition(registry)
        definitions = (definition, EXPAND_DEFINITION) if lookup else (definition,)
        system_prompt = (
            system_instructions(registry, actions_enabled=actions_available)
            + "\n\n" + LOOKUP_INSTRUCTION if lookup else research_instructions()
        )
        context.state.request_text = question
        compiled = compile_context(
            question=question,
            local_context="\n".join(filter(None, (
                local_context, server_reaction_context(context.guild),
            ))),
            context=context,
            tools=definitions,
            conversation_history=conversation_history,
            report_filter_fields=filter_fields(registry),
            system_prompt=system_prompt,
        )
        request_prompt = (
            compiled.prompt.replace(
                "<images>\nNo images.\n</images>",
                "<images>\n" + ("\n".join(
                    [image.label for image in images] + context.state.image_source_status
                ) or "No images.")
                + "\n</images>",
            ) + "\nCurrent UTC: " + datetime.now(timezone.utc).isoformat()
        )
        session = self._model.create_agent_session(
            system_prompt=system_prompt,
            prompt=request_prompt,
            tools=definitions,
            max_output_tokens=limits.AGENT_MAX_OUTPUT_TOKENS,
            **({"images": images} if images else {}),
        )
        if session is None:
            raise AgentUnavailableError("The AI backend is not configured")
        budget = ContextBudget(
            context_window_tokens=getattr(session, "context_window_tokens", None),
            output_reserve=limits.INITIAL_MAX_OUTPUT_TOKENS,
            next_input_estimate=compiled.estimated_input_tokens
            + estimate_tokens(request_prompt)
            - estimate_tokens(compiled.prompt),
            final_answer_reserve=limits.FINAL_MAX_OUTPUT_TOKENS,
            request_limits=limits.LOOKUP_LIMITS if lookup else limits.RESEARCH_LIMITS,
        )
        usage = RequestUsage()
        request_id = getattr(context.source_message, "id", None)
        rounder = ModelRounds(
            context=context,
            budget=budget,
            usage=usage,
            session=session,
            request_id=request_id,
        )
        rounder.lookup_route = lookup
        if lookup:
            def expand_context():
                instructions = research_instructions()
                registry.update(full_registry)
                expanded = plan_definition(registry)
                session.replace_tools((expanded,))
                # The provider resends the larger definition on subsequent rounds.
                budget.next_input_estimate += max(0, estimate_tokens(json.dumps(
                    expanded.parameters, ensure_ascii=False,
                )) - sum(estimate_tokens(json.dumps(item.parameters, ensure_ascii=False))
                         for item in definitions))
                budget.request_limits = limits.RESEARCH_LIMITS
                rounder.lookup_route = False
                LOGGER.info("Agent route expanded: request=%s rounds=%s", request_id, rounder.rounds)
                return instructions

            rounder.expand_context = expand_context
        LOGGER.info("Agent route: request=%s route=%s", request_id, "lookup" if lookup else "research")

        runner = PlanRunner(
            service=self,
            context=context,
            registry=registry,
            command_capabilities=command_capabilities,
            budget=budget,
            rounder=rounder,
        )

        flow = AnswerFlow(
            context=context,
            session=session,
            registry=registry,
            budget=budget,
            rounder=rounder,
            runner=runner,
            request_id=request_id,
        )
        return flow, usage

    async def answer(
        self,
        *,
        question: str,
        local_context: str,
        context: AgentRequestContext,
        conversation_history: str = "",
    ) -> str:
        record = OperatorDebugRecord(question)
        context = replace(context, operator_debug=record)
        try:
            answer = await self._answer(
                question=question, local_context=local_context, context=context,
                conversation_history=conversation_history,
            )
            record.answer = answer
            return answer
        except asyncio.CancelledError:
            record.status = "cancelled"
            raise
        except Exception:
            if record.status == "incomplete":
                record.status = "failed"
            raise
        finally:
            if context.transcript_archive is not None:
                try:
                    context.transcript_archive.enqueue_debug(context, record)
                except Exception:
                    LOGGER.exception("Agent operator debug archive enqueue failed")

    async def _answer(
        self,
        *,
        question: str,
        local_context: str,
        context: AgentRequestContext,
        conversation_history: str = "",
    ) -> str:
        from ..files.images import request_images
        context.state.request_text = question
        replay = await replay_answer(question, context)
        if replay is not None:
            context.operator_debug.status = "replayed"
            return replay
        images = await request_images(context)
        context.operator_debug.image_labels = [image.label for image in images] + context.state.image_source_status
        flow, usage = self._start_flow(
            question=question,
            local_context=local_context,
            context=context,
            conversation_history=conversation_history,
            images=images,
        )
        started_at = time.monotonic()
        status = "incomplete"
        rounder = flow.rounder
        runner = flow.runner
        try:
            answer = await flow.run()
            status = "completed"
            return answer
        except AgentGracefulEnd as error:
            await require_evidence_access(flow.context)
            status = "incomplete"
            return str(error)
        except TextGenerationError as error:
            status = "provider_error"
            raise AgentUnavailableError(str(error)) from error
        except PlanNotSettled as error:
            LOGGER.warning(
                "Agent plan not settled: request=%s reason=%s",
                getattr(flow.context.source_message, "id", None),
                error,
            )
            await require_evidence_access(flow.context)
            status = "incomplete"
            return AGENT_PLAN_UNFINISHED
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except AgentAccessLost:
            status = "access_lost"
            raise
        finally:
            context.operator_debug.status = status
            self._log_completion(
                context=flow.context,
                rounds=rounder.rounds,
                tool_calls=runner.tool_calls,
                evidence_characters=runner.evidence_characters,
                usage=usage,
                status=status,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
            )
