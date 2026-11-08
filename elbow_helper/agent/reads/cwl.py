"""CWL fact transports over the feature adapter, including retained legacy reports."""

from dataclasses import asdict
from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.cwl.config import CWL_CLAN_CODES
from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..engine.capability_contract import CapabilityContract
from elbow_helper.agent.safety.access import require_evidence_access
from ..reports.base import ArtifactCapacityError, retain_report
from elbow_helper.agent.features.cwl.report import CwlPerformanceReport
from elbow_helper.agent.engine.models import AgentRequestContext, RegisteredAgentTool
from elbow_helper.agent.reads.cache import stored_read


SCORING_CONTRACTS = {
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
        "Select completed CWL wars with read_war_attendance or read_cwl_results; "
        "use the returned war IDs. Rounds 1-7 are one season. "
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
            contract=SCORING_CONTRACTS[name],
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
        snapshot = await stored_read(context, context.cwl_queries, "result_snapshot", metric="ass",
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
        snapshot = await stored_read(context, context.cwl_queries, "result_snapshot", metric="bonus",
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


async def read_cwl_performance(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.cwl_queries is None:
        return {"error": "CWL performance data is not available."}
    if arguments.get("player_tag") is not None and normalize_player_tag(arguments["player_tag"]) is None:
        return {"error": "That player tag is not valid."}
    snapshot = await stored_read(context, context.cwl_queries, "result_snapshot", metric="performance", history_limit=arguments.get("history_limit", 3),
        season=arguments.get("season"), clan_code=arguments.get("clan_code"),
    )
    await require_evidence_access(context)
    report = CwlPerformanceReport(uuid4().hex, context.guild.id, snapshot)
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The complete CWL performance report is too large to retain in this conversation."}
    return _report_page(report, arguments)


async def read_cwl_performance_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, CwlPerformanceReport) or report.guild_id != context.guild.id:
        return {"error": "That CWL performance report is not available in this conversation. Read the performance data again."}
    result = _report_page(report, arguments)
    await require_evidence_access(context)
    return result


def _report_page(report: CwlPerformanceReport, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    player_tag = None
    if arguments.get("player_tag") is not None:
        player_tag = normalize_player_tag(arguments["player_tag"])
        if player_tag is None:
            return {"error": "That player tag is not valid."}
    return report.page(
        season=arguments.get("season"), clan_code=arguments.get("clan_code"),
        player_tag=player_tag, offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 25),
    )
