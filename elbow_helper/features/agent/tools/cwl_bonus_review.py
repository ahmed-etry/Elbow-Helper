"""Confirmed CWL bonus review decisions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..commands.outcomes import CommandOutcome, embed_text
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_CWL_BONUS_REVIEW_LINE,
    ACTION_CWL_BONUS_REVIEW_STATUS,
    ACTION_CWL_BONUS_REVIEW_MEMBER,
    ACTION_CWL_BONUS_REVIEW_SOURCE,
    ACTION_CWL_BONUS_REVIEW_LABEL,
    ACTION_CWL_BONUS_REVIEW_POST,
    ACTION_PREVIEW_BLANK,
)


def cwl_bonus_review_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="review_cwl_bonus",
        description="Complete, skip or hold a clan's existing CWL bonus review after confirmation.",
        parameters={"type": "object", "properties": {
            "clan_code": {"type": "string"},
            "mode": {"type": "string", "enum": ["review", "final"]},
            "month_key": {"type": "integer", "minimum": 1},
            "decision": {"type": "string", "enum": ["grant", "skip", "hold"]},
            "source": {"type": "string", "enum": ["scan", "link", "text"]},
            "source_text": {"type": "string"},
        }, "required": ["clan_code", "decision"], "additionalProperties": False},
    ), prepare_cwl_bonus_review, AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE, True),)


async def prepare_cwl_bonus_review(context: AgentRequestContext,
                                   values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        raise ValueError('That CWL bonus review is unavailable.')
    clan_code = values["clan_code"]
    mode = values.get("mode", "review")
    month_key = values.get("month_key") or workflow.bonus_current_month_key()
    state = workflow.bonus_review_state(clan_code, mode=mode, month_key=month_key)
    if state is None or state["closed"] or state["clan"].get("status") == "completed":
        raise ValueError('That CWL bonus review is unavailable.')
    decision = values["decision"]
    candidate = None
    source = values.get("source", "scan")
    source_text = values.get("source_text", "")
    if decision == "grant":
        candidate, issue = await workflow.prepare_bonus_review_candidate(
            clan_code, month_key=month_key, source=source, text=source_text)
        if candidate is None:
            raise ValueError(issue or 'That CWL bonus review is unavailable.')
    lines = [ACTION_CWL_BONUS_REVIEW_LINE.format(
        decision=decision, clan=clan_code, month=state["month_label"], mode=mode)]
    lines.append(ACTION_CWL_BONUS_REVIEW_STATUS.format(
        old=state["clan"].get("status", "not started"), new=decision))
    if candidate is not None:
        lines.extend(ACTION_CWL_BONUS_REVIEW_MEMBER.format(member=f"<@{member_id}>")
                     for member_id in candidate.recipient_ids)
        lines.append(ACTION_CWL_BONUS_REVIEW_SOURCE.format(
            source=candidate.source_url or source))
        preview = workflow.bonus_review_preview(candidate)
        for field in preview.fields:
            if field.name == "Rewards":
                lines.extend(line for line in str(field.value).splitlines() if line.strip())
        lines.append(ACTION_CWL_BONUS_REVIEW_POST)
        lines.extend(line if line.strip() else ACTION_PREVIEW_BLANK
                     for line in candidate.source_text.splitlines())

    async def recheck() -> bool:
        live = workflow.bonus_review_state(clan_code, mode=mode, month_key=month_key)
        if live is None or live["closed"] or live["clan"] != state["clan"]:
            return False
        if candidate is not None:
            fresh, _ = await workflow.prepare_bonus_review_candidate(
                clan_code, month_key=month_key, source=source, text=source_text)
            return fresh == candidate
        return True

    async def run() -> CommandOutcome:
        if decision == "grant":
            status, detail = await workflow.complete_bonus_review(
                state["board_key"], candidate, context.member)
            if status != "complete":
                raise ValueError(str(detail or 'That CWL bonus review is unavailable.'))
            result = workflow.bonus_review_result(candidate, detail, context.member)
            return CommandOutcome("complete", "private", text=embed_text(result))
        status = "skipped" if decision == "skip" else "on_hold"
        text = await workflow.set_bonus_review_status(
            state["board_key"], clan_code, status, context.member)
        return CommandOutcome("complete", "private", text=text)

    context.state.command_proposals.append(PreparedAction(
        "review_cwl_bonus", {"clan_code": clan_code, "mode": mode,
                              "month_key": month_key, "decision": decision},
        ChangePreview(tuple(lines), recheck, summary=ACTION_CWL_BONUS_REVIEW_LABEL),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}
