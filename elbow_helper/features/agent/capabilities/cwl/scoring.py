"""Thin agent adapters for feature-owned CWL scoring evidence."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Mapping

from elbow_helper.features.cwl.config import CWL_CLAN_CODES
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import lookup_level, ACCESS_LEAD_PLUS_OR_CWL_HELPER
from ...access import require_evidence_access
from ...models import AgentRequestContext, RegisteredAgentTool


TOOL_CONTRACTS = {
    "cwl_ass_scores": CapabilityContract(
        entity_fields=(("clan_code", "clan"),),
        required_access=frozenset({ACCESS_LEAD_PLUS_OR_CWL_HELPER}),
    ),
    "cwl_bonus_scores": CapabilityContract(
        entity_fields=(("clan_code", "clan"),),
        required_access=frozenset({ACCESS_LEAD_PLUS_OR_CWL_HELPER}),
    ),
}


def cwl_scoring_tools() -> tuple[RegisteredAgentTool, ...]:
    definitions = (
        (
            "cwl_ass_scores",
            "Calculate ASS from the exact selected stored CWL season, day (round) or war "
            "using the established scoring implementation. Observed attack averages are "
            "projected to seven attacks; preserve the returned scope and sample size and "
            "do not present a partial-scope result as a completed-season score. "
            "Return every player row.",
            {
                "clan_code": {"type": "string", "enum": list(CWL_CLAN_CODES)},
                "season": {"type": "string", "pattern": "^20\\d{2}-(0[1-9]|1[0-2])$"},
                "scope_type": {"type": "string", "enum": ["season", "round", "war"]},
                "cwl_round": {"type": "integer", "minimum": 1, "maximum": 7},
                "war_id": {"type": "string", "minLength": 1, "maxLength": 100},
            },
            ("clan_code", "season", "scope_type"),
            cwl_ass_scores,
        ),
        (
            "cwl_bonus_scores",
            "Apply the clan's existing configured CWL bonus scoring to stored completed "
            "attacks for one season, round or exact war. This returns adjusted-delta "
            "evidence, not ASS, and does not poll Clash or publish a bonus recommendation. "
            "Return every player row.",
            {
                "clan_code": {"type": "string", "enum": list(CWL_CLAN_CODES)},
                "season": {"type": "string", "pattern": "^20\\d{2}-(0[1-9]|1[0-2])$"},
                "scope_type": {"type": "string", "enum": ["season", "round", "war"]},
                "cwl_round": {"type": "integer", "minimum": 1, "maximum": 7},
                "war_tag": {"type": "string", "minLength": 1, "maxLength": 100},
            },
            ("clan_code", "season", "scope_type"),
            cwl_bonus_scores,
        ),
    )
    return tuple(
        RegisteredAgentTool(
            AgentToolDefinition(
                name=name,
                description=description,
                parameters={
                    "type": "object", "properties": properties,
                    "required": list(required), "additionalProperties": False,
                },
            ),
            handler,
            contract=TOOL_CONTRACTS[name],
            returns=(
                "players[].player_tag,ass_score" if name == "cwl_ass_scores"
                else "rows[].player_tag,adjusted_delta"
            ),
        )
        for name, description, properties, required, handler in definitions
    )


@lookup_level(ACCESS_LEAD_PLUS_OR_CWL_HELPER)
async def cwl_ass_scores(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.cwl_queries is None:
        return {"error": "CWL performance data is not available."}
    try:
        snapshot = await asyncio.to_thread(
            context.cwl_queries.ass_scope,
            clan_code=arguments["clan_code"], season=arguments["season"],
            scope_type=arguments["scope_type"],
            cwl_round=arguments.get("cwl_round"),
            war_id=arguments.get("war_id"),
        )

    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    result = asdict(snapshot)
    result.pop("rows", None)
    result["players"] = [asdict(row) for row in snapshot.rows]
    return result


@lookup_level(ACCESS_LEAD_PLUS_OR_CWL_HELPER)
async def cwl_bonus_scores(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.cwl_queries is None:
        return {"error": "CWL bonus scoring is not available."}
    try:
        snapshot = await asyncio.to_thread(
            context.cwl_queries.bonus_scope,
            clan_code=arguments["clan_code"], season=arguments["season"],
            scope_type=arguments["scope_type"],
            cwl_round=arguments.get("cwl_round"),
            war_tag=arguments.get("war_tag"),
        )

    except (RuntimeError, ValueError) as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    result = asdict(snapshot)
    result.pop("rows", None)
    result.pop("summaries", None)
    result.pop("attacks", None)
    result["rows"] = [
        asdict(row)
        for row in (snapshot.summaries if snapshot.scope_type == "season" else snapshot.attacks)
    ]
    result.update(
        metric_name="Configured CWL bonus adjusted delta",
        metric_definition=(
            "actual_contribution - configured_th_expectation + configured_hit_adjustment"
        ),
        ass_distinction={"is_ass": False},
    )
    return result


__all__ = ["cwl_scoring_tools", "cwl_ass_scores", "cwl_bonus_scores"]
