"""Bounded orchestration for the agent."""

from __future__ import annotations

import logging

import asyncio
from datetime import datetime, timezone
import time
from elbow_helper.infrastructure.ai import AgentModel
from elbow_helper.infrastructure.ai import TextGenerationError
from ..models import AgentRequestContext
from ..actions.contracts import ActionClass
from ..access import AgentAccessLost
from ..disclosure import require_disclosure_access
from ..prompts import SYSTEM_PROMPT
from ..conversation.context import compile_context, estimate_tokens
from .registry import build_agent_tools
from .usage import RequestUsage
from .budgets import ContextBudget
from ..reports.tools import filter_fields
from ..plan.format import plan_definition, system_instructions
from ..plan.planning import PlanNotSettled
from ..plan.sources import named_sources
from ..plan.scope import ScopeLedger
from ..capabilities import enabled_adapters
from ..commands.bridge import build_command_tools, check_command_plan
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

    def __init__(self, model: AgentModel, *, actions_enabled: bool = True) -> None:
        self._model = model
        self._actions_enabled = actions_enabled

    execute_tool = staticmethod(execute_tool)

    _execute_tool = execute_tool

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
                'Agent usage: request=%s status=%s '
                'elapsed_ms=%s invoker=%s channel=%s '
                'rounds=%s tools=%s evidence_chars=%s '
                'prompt_tokens=%s completion_tokens=%s '
                'cache_hit_tokens=%s cache_miss_tokens=%s '
                'attempted_rounds=%s unknown_token_rounds=%s '
                'unknown_cache_rounds=%s'
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
    ):
        registry = build_agent_tools()
        command_capabilities = {}
        actions_available = self._actions_enabled and getattr(context.bot, "tree", None) is not None
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
        visible_channels = (
            *getattr(context.guild, "channels", ()),
            *getattr(context.guild, "threads", ()),
        )
        sources = named_sources(question, visible_channels)
        command_check = lambda plan: check_command_plan(plan, command_capabilities, sources)
        definition = plan_definition(registry)
        system_prompt = system_instructions(registry, actions_enabled=actions_available)
        context.state.request_text = question
        compiled = compile_context(
            question=question,
            local_context=local_context,
            context=context,
            tools=(definition,),
            conversation_history=conversation_history,
            report_filter_fields=filter_fields(registry),
        )
        request_prompt = (
            compiled.prompt + "\nCurrent UTC: " + datetime.now(timezone.utc).isoformat()
        )
        session = self._model.create_agent_session(
            system_prompt=system_prompt,
            prompt=request_prompt,
            tools=(definition,),
            max_output_tokens=limits.AGENT_MAX_OUTPUT_TOKENS,
        )
        if session is None:
            raise AgentUnavailableError("The AI backend is not configured")
        budget = ContextBudget(
            context_window_tokens=getattr(session, "context_window_tokens", None),
            output_reserve=limits.INITIAL_MAX_OUTPUT_TOKENS,
            next_input_estimate=compiled.estimated_input_tokens
            + estimate_tokens(system_prompt)
            - estimate_tokens(SYSTEM_PROMPT)
            + estimate_tokens(request_prompt)
            - estimate_tokens(compiled.prompt),
            final_answer_reserve=limits.FINAL_MAX_OUTPUT_TOKENS,
        )
        usage = RequestUsage()
        request_id = getattr(context.source_message, "id", None)
        ledger = ScopeLedger(context)
        rounder = ModelRounds(
            context=context,
            budget=budget,
            usage=usage,
            session=session,
            request_id=request_id,
            ledger=ledger,
        )

        runner = PlanRunner(
            service=self,
            context=context,
            registry=registry,
            command_capabilities=command_capabilities,
            sources=sources,
            budget=budget,
            ledger=ledger,
            rounder=rounder,
        )

        flow = AnswerFlow(
            context=context,
            session=session,
            registry=registry,
            sources=sources,
            command_check=command_check,
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
        flow, usage = self._start_flow(
            question=question,
            local_context=local_context,
            context=context,
            conversation_history=conversation_history,
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
            await require_disclosure_access(flow.context)
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
            await require_disclosure_access(flow.context)
            status = "incomplete"
            return AGENT_PLAN_UNFINISHED
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except AgentAccessLost:
            status = "access_lost"
            raise
        finally:
            self._log_completion(
                context=flow.context,
                rounds=rounder.rounds,
                tool_calls=runner.tool_calls,
                evidence_characters=runner.evidence_characters,
                usage=usage,
                status=status,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
            )
