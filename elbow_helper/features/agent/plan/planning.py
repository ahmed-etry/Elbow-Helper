"""Read one direct reply or one checked request plan."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolResult
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort, AgentSession

from ..models import RegisteredAgentTool
from ..engine.budgets import INITIAL_MAX_OUTPUT_TOKENS
from .checker import PlanCheck, check_plan
from .format import PLAN_TOOL_NAME, REPLY_CAPABILITIES
from .results import plan_feedback
from ..prompts import (
    ONE_PLAN_INSTRUCTION,
)


LOGGER = logging.getLogger(__name__)


class PlanNotSettled(ValueError):
    """The model gave neither a reply nor a checked plan after its correction."""


def social_reply_issue(plan, content):
    if (plan["steps"] and not (plan.get("reply") or (content or "").strip())
            and all(item["capability"] == "react_to_request" for item in plan["steps"])):
        return "Include reply text with reactions, or select a GIF."
    return ""


def reuse_reply_target(plan, completed, *, reply_to=None, ping_author=False):
    """Omit an identical delivery target already set by a completed plan.

    This only removes redundant presentation work when reply text is present.
    Reads, changes and changed targets retain ordinary execution and ID checks.
    """
    if not isinstance(plan, dict) or not plan.get("reply") or not isinstance(plan.get("steps"), list):
        return plan
    steps = []
    for step in plan["steps"]:
        previous = completed.get(step.get("id")) if isinstance(step, dict) else None
        if (previous and step.get("capability") == "set_discord_reply_target"
                and isinstance(step.get("arguments"), dict)
                and step.get("arguments", {}).get("message_id") == reply_to
                and step.get("arguments", {}).get("ping_author", False) == ping_author
                and all(step.get(key) == previous.get(key)
                        for key in ("capability", "arguments", "depends_on"))):
            continue
        steps.append(step)
    return {**plan, "steps": steps}


def request_plan(step, direct_capabilities=()):
    """Adapt native social calls to the same checked plan as explicit planning."""
    if len(step.tool_calls) == 1 and step.tool_calls[0].name == PLAN_TOOL_NAME:
        try:
            plan = json.loads(step.tool_calls[0].arguments)
            # IDs are executor bookkeeping. Supplying a missing, unreferenced
            # ID does not change arguments, dependencies or authorization and
            # avoids resending the entire research context for that typo.
            if isinstance(plan, dict) and isinstance(plan.get("steps"), list):
                # Reply text is presentation, never an executable instruction.
                # Models sometimes include an acknowledgement or placeholder
                # beside research. Discard it and execute the checked steps;
                # the answer is written only after their results are available.
                if isinstance(plan.get("reply"), str) and (
                    not plan["reply"].strip()
                    or any(isinstance(item, dict)
                           and item.get("capability") not in REPLY_CAPABILITIES
                           for item in plan["steps"])
                ):
                    plan.pop("reply")
                used = {item["id"] for item in plan["steps"]
                        if isinstance(item, dict) and isinstance(item.get("id"), str)}
                for index, item in enumerate(plan["steps"], 1):
                    if isinstance(item, dict) and "id" not in item:
                        identity = f"step_{index}"
                        while identity in used:
                            identity += "_auto"
                        item["id"] = identity
                        used.add(identity)
            return plan
        except (TypeError, ValueError):
            return None
    if (not step.tool_calls or any(call.name not in direct_capabilities for call in step.tool_calls)
            or sum(call.name == "find_gif" for call in step.tool_calls) > 1):
        return None
    try:
        steps = [{"id": f"social_{index}_{call.call_id[-24:]}", "capability": call.name,
                  "arguments": json.loads(call.arguments), "depends_on": []}
                 for index, call in enumerate(step.tool_calls, 1)]
    except (TypeError, ValueError):
        return None
    return {"goal": "Reply to the social exchange", "effort": "low", "output": "text", "steps": steps}


@dataclass(frozen=True, slots=True)
class ReadDecision:
    answer: str | None
    plan: dict[str, Any] | None
    rounds: tuple[Any, ...]
    self_target: bool = False


async def read_request(
    session: AgentSession, registry: Mapping[str, RegisteredAgentTool],
    request_id: int | None,
    validate_step: Callable[[Mapping[str, Any]], Awaitable[str]] | None = None,
    validate_plan: Callable[[Mapping[str, Any]], str] | None = None,
    advance: Callable[..., Awaitable[Any]] | None = None,
    on_check: Callable[[str, PlanCheck], None] | None = None,
    reasoning_effort: AgentReasoningEffort = AgentReasoningEffort.HIGH,
    validate_answer: Callable[[str, Mapping[str, Any] | None], str] | None = None,
    direct_capabilities: Callable[[], frozenset[str]] | None = None,
    initial_step: Any = None,
) -> ReadDecision:
    rounds = []
    pending: tuple[AgentToolResult, ...] = ()
    continuation = None
    for attempt in range(2):
        step = initial_step if initial_step is not None else await (advance or session.advance)(
            pending, reasoning_effort=reasoning_effort,
            max_output_tokens=INITIAL_MAX_OUTPUT_TOKENS,
            **({"continuation_instruction": continuation} if continuation else {}),
        )
        initial_step = None
        rounds.append(step)
        if not step.tool_calls:
            if step.content:
                issue = validate_answer(step.content, None) if validate_answer is not None else ""
                if not issue:
                    return ReadDecision(step.content, None, tuple(rounds))
                if attempt:
                    raise PlanNotSettled(issue)
                pending, continuation = (), issue
                continue
            raise PlanNotSettled("A corrected plan is required.")
        available = direct_capabilities() if direct_capabilities is not None else frozenset()
        plan = request_plan(step, available)
        if plan is None and (len(step.tool_calls) != 1 or step.tool_calls[0].name != PLAN_TOOL_NAME):
            issue = ONE_PLAN_INSTRUCTION
            step_id, offered = "", ()
            if on_check is not None:
                for call in step.tool_calls:
                    on_check(call.call_id, PlanCheck(False, issue))
        else:
            check = check_plan(plan, registry)
            if plan is None:
                check = PlanCheck(False, 'Plan arguments must be a valid JSON object. Resubmit one valid '
                    'submit_request_plan call. No reads ran; missing results do not establish an access restriction.')
            if check.ok and (issue := social_reply_issue(plan, step.content)):
                check = PlanCheck(False, issue)
            if check.ok and validate_plan is not None:
                issue = validate_plan(plan)
                if issue:
                    check = type(check)(False, issue)
            if check.ok and plan.get("reply") and validate_answer is not None:
                if issue := validate_answer(plan["reply"], plan):
                    check = type(check)(False, issue)
            if check.ok and validate_step is not None:
                for planned_step in plan["steps"]:
                    if planned_step["id"] in check.step_errors:
                        continue
                    issue = await validate_step(planned_step)
                    if issue:
                        check = type(check)(False, issue, planned_step["id"])
                        break
            LOGGER.info("Agent plan: request=%s attempt=%s plan=%s", request_id, attempt + 1,
                        json.dumps(plan, ensure_ascii=False, default=str))
            LOGGER.info("Agent plan check: request=%s attempt=%s ok=%s step=%s error=%s",
                        request_id, attempt + 1, check.ok, check.step_id, check.error)
            if on_check is not None:
                for call in step.tool_calls:
                    on_check(call.call_id, check)
            if check.ok:
                if plan.get("reply"):
                    rounds[-1] = replace(step, content=plan["reply"])
                    if not plan["steps"]:
                        return ReadDecision(plan["reply"], None, tuple(rounds), plan.get("self_target", False))
                return ReadDecision(None, plan, tuple(rounds), plan.get("self_target", False))
            issue = check.error
            step_id, offered = check.step_id, check.offered
        if attempt:
            raise PlanNotSettled(issue)
        pending = tuple(AgentToolResult(call.call_id, json.dumps(plan_feedback(
            issue, step_id=step_id, offered=offered,
        ))) for call in step.tool_calls)
        continuation = None
    raise PlanNotSettled("A checked plan is required.")
