"""Read one direct reply or one checked request plan."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolResult
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort, AgentSession

from ..models import RegisteredAgentTool
from .checker import check_plan
from .format import PLAN_TOOL_NAME
from .results import plan_feedback


LOGGER = logging.getLogger(__name__)


class PlanNotSettled(ValueError):
    """The model gave neither a reply nor a checked plan after its correction."""


@dataclass(frozen=True, slots=True)
class ReadDecision:
    answer: str | None
    plan: dict[str, Any] | None
    rounds: tuple[Any, ...]


async def read_request(
    session: AgentSession, registry: Mapping[str, RegisteredAgentTool],
    named_sources: Mapping[str, frozenset[Any]], request_id: int | None,
    validate_step: Callable[[Mapping[str, Any]], Awaitable[str]] | None = None,
    validate_plan: Callable[[Mapping[str, Any]], str] | None = None,
    advance: Callable[..., Awaitable[Any]] | None = None,
) -> ReadDecision:
    rounds = []
    pending: tuple[AgentToolResult, ...] = ()
    for attempt in range(2):
        step = await (advance or session.advance)(
            pending, reasoning_effort=AgentReasoningEffort.LOW,
            max_output_tokens=8000,
        )
        rounds.append(step)
        if not step.tool_calls:
            if step.content:
                return ReadDecision(step.content, None, tuple(rounds))
            raise PlanNotSettled("A corrected plan is required.")
        if len(step.tool_calls) != 1 or step.tool_calls[0].name != PLAN_TOOL_NAME:
            issue = "Submit one request plan."
            step_id, offered = "", ()
        else:
            try:
                plan = json.loads(step.tool_calls[0].arguments)
            except (TypeError, ValueError):
                plan = None
            check = check_plan(plan, registry, named_sources)
            if check.ok and validate_plan is not None:
                issue = validate_plan(plan)
                if issue:
                    check = type(check)(False, issue)
            if check.ok and validate_step is not None:
                for planned_step in plan["steps"]:
                    issue = await validate_step(planned_step)
                    if issue:
                        check = type(check)(False, issue, planned_step["id"])
                        break
            LOGGER.info("Agent plan: request=%s attempt=%s plan=%s", request_id, attempt + 1,
                        json.dumps(plan, ensure_ascii=False, default=str))
            LOGGER.info("Agent plan check: request=%s attempt=%s ok=%s step=%s error=%s",
                        request_id, attempt + 1, check.ok, check.step_id, check.error)
            if check.ok:
                return ReadDecision(None, plan, tuple(rounds))
            issue = check.error
            step_id, offered = check.step_id, check.offered
        if attempt:
            raise PlanNotSettled(issue)
        pending = tuple(AgentToolResult(call.call_id, json.dumps(plan_feedback(
            issue, step_id=step_id, offered=offered,
        ))) for call in step.tool_calls)
    raise PlanNotSettled("A checked plan is required.")
