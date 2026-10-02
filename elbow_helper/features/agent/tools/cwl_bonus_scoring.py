"""Confirmed CWL bonus scoring edits from the settings panel."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..actions.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_BONUS_SCORING_LINE,
    ACTION_FIELD_CHANGE,
    ACTION_BONUS_SCORING_DONE,
    ACTION_BONUS_SCORING_LABEL,
    ACTION_BONUS_SCORING_COPY_LINE,
    ACTION_BONUS_SCORING_RESTORE,
)


_ADJUSTMENTS = (
    "uphit_bonus_per_level", "downhit_penalty_per_level",
    "downhit_severe_after", "downhit_severe_base",
    "downhit_severe_multiplier",
)


def cwl_bonus_scoring_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="set_cwl_bonus_scoring",
        description="Set a CWL Expected Score, TH Adjustments, or copy a clan scoring setup after confirmation.",
        parameters={"type": "object", "properties": {
            "clan_code": {"type": "string"},
            "operation": {"type": "string", "enum": ["score", "adjustments", "copy"]},
            "source_clan": {"type": "string"},
            "attacker_th": {"type": "integer", "minimum": 1},
            "defender_th": {"type": "integer", "minimum": 1},
            "score": {"type": "number", "minimum": 0, "maximum": 3},
            **{field: {"type": "integer" if field == "downhit_severe_after" else "number"}
               for field in _ADJUSTMENTS},
        }, "required": ["clan_code", "operation"], "additionalProperties": False},
    ), prepare_cwl_bonus_scoring, AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE, True),)


def _changed_lines(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    lines = []
    old_scores = before.get("matchup_expected") or {}
    new_scores = after.get("matchup_expected") or {}
    for key in sorted(set(old_scores) | set(new_scores)):
        if old_scores.get(key) != new_scores.get(key):
            lines.append(ACTION_FIELD_CHANGE.format(
                field=f"TH {key.replace(':', ' vs ')} Expected Score",
                old=old_scores.get(key), new=new_scores.get(key)))
    for field in _ADJUSTMENTS:
        if before.get(field) != after.get(field):
            lines.append(ACTION_FIELD_CHANGE.format(
                field=field.replace("_", " ").title(),
                old=before.get(field), new=after.get(field)))
    return lines


async def prepare_cwl_bonus_scoring(context: AgentRequestContext,
                                    values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        raise ValueError('That CWL bonus scoring setup is unavailable.')
    clan = str(values["clan_code"]).upper()
    before, _, revision = workflow.bonus_scoring_snapshot(clan)
    after = copy.deepcopy(before)
    operation = values["operation"]
    source = None
    if operation == "score":
        required = ("attacker_th", "defender_th", "score")
        if any(field not in values for field in required):
            return {"status": "needs_input", "missing": list(required), "prepared_count": 0}
        key = f"{values['attacker_th']}:{values['defender_th']}"
        if key not in after.get("matchup_expected", {}):
            raise ValueError('That CWL bonus scoring setup is unavailable.')
        after["matchup_expected"][key] = float(values["score"])
    elif operation == "adjustments":
        changes = {field: values[field] for field in _ADJUSTMENTS if field in values}
        if not changes:
            return {"status": "needs_input", "missing": list(_ADJUSTMENTS),
                    "prepared_count": 0}
        after.update(changes)
    elif operation == "copy":
        source = str(values.get("source_clan") or "").upper()
        if not source or source == clan:
            return {"status": "needs_input", "missing": ["source_clan"],
                    "prepared_count": 0}
        after, _, source_revision = workflow.bonus_scoring_snapshot(source)
        if source_revision != revision:
            raise ValueError('That CWL bonus scoring setup is unavailable.')
    else:
        raise ValueError('That CWL bonus scoring setup is unavailable.')
    errors = workflow.bonus_scoring_issues(clan, after)
    if errors:
        raise ValueError("\n".join(errors))
    differences = _changed_lines(before, after)
    if not differences:
        return {"status": "no_change", "prepared_count": 0}
    lines = [(ACTION_BONUS_SCORING_COPY_LINE.format(source=source, clan=clan)
              if source is not None else ACTION_BONUS_SCORING_LINE.format(clan=clan)),
             *differences]

    async def recheck() -> bool:
        current, _, live_revision = workflow.bonus_scoring_snapshot(clan)
        if live_revision != revision or current != before:
            return False
        if source is not None:
            source_current, _, _ = workflow.bonus_scoring_snapshot(source)
            return source_current == after
        return True

    async def run() -> CommandOutcome:
        if source is not None:
            updated = workflow.copy_bonus_scoring(
                source, clan, context.member, expected_revision=revision)
        else:
            updated = workflow.save_bonus_scoring(
                clan, after, context.member, expected_revision=revision,
                summary="; ".join(differences),
            )
        return CommandOutcome(
            "complete", "private", text=ACTION_BONUS_SCORING_DONE.format(clan=clan),
            after={"payload": copy.deepcopy(updated["clans"][clan])},
        )

    context.state.command_proposals.append(PreparedAction(
        "set_cwl_bonus_scoring", {"clan_code": clan},
        ChangePreview(tuple(lines), recheck, summary=ACTION_BONUS_SCORING_LABEL,
                      before={"payload": before}), run,
    ))
    return {"status": "confirmation_required"}


async def prepare_cwl_bonus_scoring_undo(context: AgentRequestContext,
                                         log: Mapping[str, Any]) -> PreparedAction:
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        raise ValueError('That CWL bonus scoring setup is unavailable.')
    clan = log["targets"]["clan_code"]
    current, _, revision = workflow.bonus_scoring_snapshot(clan)
    expected = log["after"]["payload"]
    prior = log["before"]["payload"]
    if current != expected:
        raise ValueError('That CWL bonus scoring setup is unavailable.')
    lines = [ACTION_BONUS_SCORING_LINE.format(clan=clan),
             *_changed_lines(current, prior)]

    async def recheck() -> bool:
        live, _, live_revision = workflow.bonus_scoring_snapshot(clan)
        return live_revision == revision and live == expected

    async def run() -> CommandOutcome:
        updated = workflow.save_bonus_scoring(
            clan, prior, context.member, expected_revision=revision,
            summary=ACTION_BONUS_SCORING_RESTORE,
        )
        return CommandOutcome(
            "complete", "private", text=ACTION_BONUS_SCORING_DONE.format(clan=clan),
            after={"payload": copy.deepcopy(updated["clans"][clan])},
        )

    return PreparedAction(
        "undo_cwl_bonus_scoring", {"clan_code": clan},
        ChangePreview(tuple(lines), recheck, summary=ACTION_BONUS_SCORING_LABEL,
                      before={"payload": expected}), run,
    )
