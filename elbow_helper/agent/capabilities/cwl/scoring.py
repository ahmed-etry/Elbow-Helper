"""Thin agent adapters for feature-owned CWL scoring evidence."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Mapping

from elbow_helper.features.cwl.config import CWL_CLAN_CODES
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import require_evidence_access
from ...models import AgentRequestContext, RegisteredAgentTool


TOOL_CONTRACTS = {
    "cwl_ass_scores": CapabilityContract(
        entity_fields=(("clan_code", "clan"),),
    ),
    "cwl_bonus_scores": CapabilityContract(
        entity_fields=(("clan_code", "clan"),),
    ),
}


def cwl_scoring_tools() -> tuple[RegisteredAgentTool, ...]:
    selection = {
        "clan_code": {"type": "string", "enum": list(CWL_CLAN_CODES)},
        "war_ids": {
            "type": "array", "minItems": 1, "maxItems": 56, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 100},
        },
    }
    pick_wars = (
        "query_bot_data selects health.wars IDs: clan_code, war_type=CWL, cwl_season, "
        "cwl_round (days 1-7), state=warEnded. "
    )
    # Both feature scorers calculate exactly the selected wars as one result.
    # ASS projects observed averages to seven attacks; a partial sample is not a full season.
    definitions = (
        (
            "cwl_ass_scores",
            "Calculate one combined CWL ASS result for chosen war IDs.",
            pick_wars + "Sample projects to seven attacks.",
            selection, ("clan_code", "war_ids"), cwl_ass_scores,
        ),
        (
            "cwl_bonus_scores",
            "Apply the clan's CWL bonus scoring to chosen war IDs.",
            pick_wars + "Adjusted delta is not ASS.",
            selection, ("clan_code", "war_ids"), cwl_bonus_scores,
        ),
    )
    return tuple(
        RegisteredAgentTool(
            AgentToolDefinition(
                name=name,
                description=description, usage_note=note,
                parameters={
                    "type": "object", "properties": properties,
                    "required": list(required), "additionalProperties": False,
                },
            ),
            handler,
            contract=TOOL_CONTRACTS[name],
        )
        for name, description, note, properties, required, handler in definitions
    )


async def cwl_ass_scores(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.cwl_queries is None:
        return {"error": "CWL performance data is not available."}
    try:
        snapshot = await asyncio.to_thread(
            context.cwl_queries.ass_wars,
            clan_code=arguments["clan_code"], war_ids=arguments["war_ids"],
        )

    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    result = asdict(snapshot)
    result = {"players": list(result.pop("rows")), **result}
    result["ranking"] = ass_ranking(result["players"])
    return result


def ass_ranking(players):
    """Project every selected account without choosing how to answer a request.

    Text is atomic at the result-size boundary: detailed player/attack arrays
    may be trimmed, but a surviving ranking is never a partial list marked
    complete. Keep feature-owned ranks, including ties and unscored accounts.
    """
    lines = ["Rank\tAccount\tPlayer tag\tASS"]
    for row in sorted(players, key=lambda row: (
        row["rank"] is None, row["rank"] or 0, row["player_tag"],
    )):
        rank = str(row["rank"]) if row["rank"] is not None else "-"
        score = f"{row['ass_score']:.2f}" if row["ass_score"] is not None else "-"
        name = " ".join(row["player_name"].split())
        lines.append(f"{rank}\t{name}\t{row['player_tag']}\t{score}")
    return {"account_count": len(players), "complete": True, "text": "\n".join(lines)}


async def cwl_bonus_scores(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.cwl_queries is None:
        return {"error": "CWL bonus scoring is not available."}
    try:
        snapshot = await asyncio.to_thread(
            context.cwl_queries.bonus_wars,
            clan_code=arguments["clan_code"], war_ids=arguments["war_ids"],
        )

    except (RuntimeError, ValueError) as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    result = asdict(snapshot)
    result = {"rows": list(result.pop("summaries")), **result}
    result["attack_sample"] = result.pop("attacks")
    result.update(
        metric_name="Configured CWL bonus adjusted delta",
        metric_definition=(
            "actual_contribution - configured_th_expectation + configured_hit_adjustment"
        ),
        ass_distinction={"is_ass": False},
    )
    return result


__all__ = ["cwl_scoring_tools", "cwl_ass_scores", "cwl_bonus_scores"]
