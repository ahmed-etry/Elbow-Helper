"""Confirmed CWL bonus scoring edits from the settings panel."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.features.cwl.bonus.settings import ADJUSTMENT_FIELDS

from ...engine.capability_contract import CapabilityContract
from ...access import ACCESS_LEAD, require_evidence_access
from ...actions.contracts import ActionRefused, ActionClass, ChangePreview, PreparedAction
from ...actions.outcomes import ActionOutcome
from ...actions.values import display_value
from ...models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ...wording import (
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
        ActionClass.CHANGE,
        contract=CapabilityContract(
            entity_fields=(("clan_code", "clan"), ("source_clan", "clan")),
            source_scope="request_context", required_access=frozenset({ACCESS_LEAD}),
            filter_fields=(
                "operation",
                "attacker_th",
                "defender_th",
                "score",
                "uphit_bonus_per_level",
                "downhit_penalty_per_level",
                "downhit_severe_after",
                "downhit_severe_base",
                "downhit_severe_multiplier",
            )),
            ),)


def _changed_lines(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    lines = []
    old_scores = before.get("matchup_expected") or {}
    new_scores = after.get("matchup_expected") or {}
    for key in sorted(set(old_scores) | set(new_scores)):
        if old_scores.get(key) != new_scores.get(key):
            lines.append(ACTION_FIELD_CHANGE.format(
                field=f"TH {key.replace(':', ' vs ')} Expected Score",
                old=display_value(old_scores.get(key)), new=display_value(new_scores.get(key))))
    for field, label in ADJUSTMENT_FIELDS:
        if before.get(field) != after.get(field):
            lines.append(ACTION_FIELD_CHANGE.format(
                field=label,
                old=display_value(before.get(field)), new=display_value(after.get(field))))
    return lines


async def prepare_cwl_bonus_scoring(context: AgentRequestContext,
                                    values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        raise ActionRefused('That CWL bonus scoring setup is unavailable.')
    clan = str(values["clan_code"]).upper()
    before, _, revision = workflow.bonus_scoring_snapshot(clan)
    after = copy.deepcopy(before)
    operation = values["operation"]
    source = None
    if operation == "score":
        required = ("attacker_th", "defender_th", "score")
        if any(field not in values for field in required):
            return {"status": "needs_input", "missing": list(required)}
        key = f"{values['attacker_th']}:{values['defender_th']}"
        if key not in after.get("matchup_expected", {}):
            raise ActionRefused('That CWL bonus scoring setup is unavailable.')
        after["matchup_expected"][key] = float(values["score"])
    elif operation == "adjustments":
        changes = {field: values[field] for field in _ADJUSTMENTS if field in values}
        if not changes:
            return {"status": "needs_input", "missing": list(_ADJUSTMENTS)}
        after.update(changes)
    elif operation == "copy":
        source = str(values.get("source_clan") or "").upper()
        if not source or source == clan:
            return {"status": "needs_input", "missing": ["source_clan"]}
        after, _, source_revision = workflow.bonus_scoring_snapshot(source)
        if source_revision != revision:
            raise ActionRefused('That CWL bonus scoring setup is unavailable.')
    else:
        raise ActionRefused('That CWL bonus scoring setup is unavailable.')
    errors = workflow.bonus_scoring_issues(clan, after)
    if errors:
        raise ActionRefused("\n".join(errors))
    differences = _changed_lines(before, after)
    if not differences:
        return {"status": "no_change"}
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

    async def run() -> ActionOutcome:
        if source is not None:
            updated = workflow.copy_bonus_scoring(
                source, clan, context.member, expected_revision=revision)
        else:
            updated = workflow.save_bonus_scoring(
                clan, after, context.member, expected_revision=revision,
                summary="; ".join(differences),
            )
        return ActionOutcome(
            "complete", "private", text=ACTION_BONUS_SCORING_DONE.format(clan=clan),
            after={"payload": copy.deepcopy(updated["clans"][clan])},
        )

    context.state.proposed_changes.append(PreparedAction(
        "set_cwl_bonus_scoring", {"clan_code": clan},
        ChangePreview(tuple(lines[:1]), recheck, summary=ACTION_BONUS_SCORING_LABEL,
                      details=tuple(lines[1:]), detail_access=frozenset({ACCESS_LEAD}),
                      before={"payload": before}), run,
    ))
    return {"status": "confirmation_required"}


async def prepare_cwl_bonus_scoring_undo(context: AgentRequestContext,
                                         log: Mapping[str, Any]) -> PreparedAction:
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        raise ActionRefused('That CWL bonus scoring setup is unavailable.')
    clan = log["targets"]["clan_code"]
    current, _, revision = workflow.bonus_scoring_snapshot(clan)
    expected = log["after"]["payload"]
    prior = log["before"]["payload"]
    if current != expected:
        raise ActionRefused('That CWL bonus scoring setup is unavailable.')
    lines = [ACTION_BONUS_SCORING_LINE.format(clan=clan),
             *_changed_lines(current, prior)]

    async def recheck() -> bool:
        live, _, live_revision = workflow.bonus_scoring_snapshot(clan)
        return live_revision == revision and live == expected

    async def run() -> ActionOutcome:
        updated = workflow.save_bonus_scoring(
            clan, prior, context.member, expected_revision=revision,
            summary=ACTION_BONUS_SCORING_RESTORE,
        )
        return ActionOutcome(
            "complete", "private", text=ACTION_BONUS_SCORING_DONE.format(clan=clan),
            after={"payload": copy.deepcopy(updated["clans"][clan])},
        )

    return PreparedAction(
        "undo_cwl_bonus_scoring", {"clan_code": clan},
        ChangePreview(tuple(lines[:1]), recheck, summary=ACTION_BONUS_SCORING_LABEL,
                      details=tuple(lines[1:]), detail_access=frozenset({ACCESS_LEAD}),
                      before={"payload": expected}), run,
    )


UNDO_HANDLERS = {
    'set_cwl_bonus_scoring': prepare_cwl_bonus_scoring_undo,
    'undo_cwl_bonus_scoring': prepare_cwl_bonus_scoring_undo,
}
