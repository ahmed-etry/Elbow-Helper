"""Route retained reports through their owning readers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool


REPORT_READERS = {
    "discord_research": "read_discord_research_report",
    "role_accounts": "read_role_account_report",
    "achievement_progress": "read_member_achievement_report",
    "achievement_leaderboard": "read_achievement_leaderboard_report",
    "coin_transactions": "read_member_coin_history_report",
    "raffle": "read_raffle_report",
    "event_schedule": "read_event_schedule_report",
    "missing_elder": "read_missing_elder_report",
    "clan_health": "read_clan_health_report",
    "family_account_movements": "read_family_account_movement_report",
    "regular_war": "read_regular_war_report",
    "historical_regular_wars": "read_historical_regular_war_report",
    "roster_signups": "read_roster_report",
    "cwl_performance": "read_cwl_performance_report",
    "cwl_ass_scope": "read_cwl_ass_scope_report",
    "cwl_bonus_scope": "read_cwl_bonus_scope_report",
    "pending_transfer_requests": "read_pending_transfer_report",
    "member_lifecycle": "read_member_lifecycle_report",
    "active_hibernation": "read_active_hibernation_report",
    "support_ticket_inventory": "read_support_ticket_report",
    "active_recruitment_trials": "read_active_recruitment_trial_report",
    "examination_case_status": "read_examination_case_report",
    "active_leadership_records": "read_leadership_record_report",
    "csv_import": "read_csv_import",
    "xlsx_import": "read_xlsx_import",
    "text_import": "read_text_import",
    "approved_knowledge": "read_approved_knowledge_report",
}
REPORT_COMPARERS = {
    "role_accounts": "compare_role_account_reports",
    "clan_health": "compare_clan_health_reports",
    "roster_signups": "compare_roster_reports",
}
READ_NAME = "read_saved_report"
COMPARE_NAME = "compare_saved_reports"

def original_tool(registry: Mapping[str, RegisteredAgentTool], name: str,
                  arguments: Mapping[str, Any]) -> RegisteredAgentTool | None:
    kind = arguments.get("report_kind")
    handler = _router(registry, name)
    if not isinstance(kind, str) or not isinstance(handler, ReportRouter):
        return None
    return handler.specs.get(kind)


def saved_report_contracts(registry):
    return {tool.definition.name: tool.contract
            for name in (READ_NAME, COMPARE_NAME)
            for tool in _router(registry, name).specs.values()}


def original_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in arguments.items() if key != "report_kind"}


def filter_fields(registry: Mapping[str, RegisteredAgentTool]) -> dict[str, tuple[str, ...]]:
    handler = _router(registry, READ_NAME)
    if not isinstance(handler, ReportRouter):
        return {}
    return {kind: tuple(sorted(set(tool.definition.parameters["properties"]) -
                               {"report_id"})) for kind, tool in handler.specs.items()}


def _router(registry: Mapping[str, RegisteredAgentTool], name: str) -> Any:
    handler = getattr(registry.get(name), "handler", None)
    return getattr(handler, "__wrapped__", handler)


def unsupported_fields(selected: RegisteredAgentTool,
                       arguments: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(set(original_arguments(arguments)) -
                        set(selected.definition.parameters["properties"])))


def unsupported_field_error(selected: RegisteredAgentTool,
                            arguments: Mapping[str, Any]) -> str:
    extra = unsupported_fields(selected, arguments)
    supported = tuple(sorted(set(selected.definition.parameters["properties"]) -
                             {"report_id", "before_report_id", "after_report_id"}))
    return (f"Unsupported field: {', '.join(extra)}. Supported filters: "
            f"{', '.join(supported) if supported else 'none'}.")


@dataclass(frozen=True, slots=True)
class ReportRouter:
    name: str
    specs: Mapping[str, RegisteredAgentTool]

    async def __call__(self, context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        kind = arguments.get("report_kind")
        selected = self.specs.get(kind) if isinstance(kind, str) else None
        if selected is None:
            return {"error": "That report kind is unavailable."}
        extra = unsupported_fields(selected, arguments)
        if extra:
            return {"error": unsupported_field_error(selected, arguments)}
        ids = (("before_report_id", "after_report_id") if self.name == COMPARE_NAME
               else ("report_id",))
        for field in ids:
            report = context.state.reports.get(arguments[field])
            if report is None or report.manifest().get("kind") != kind:
                return {"error": "That report is unavailable in this conversation."}
        return await selected.handler(context, original_arguments(arguments))


def _properties(specs: Mapping[str, RegisteredAgentTool]) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for tool in specs.values():
        for key, schema in tool.definition.parameters["properties"].items():
            if key in ("report_id", "before_report_id", "after_report_id"):
                continue
            if key not in fields:
                fields[key] = dict(schema)
                continue
            current = fields[key]
            if current.get("type") != schema.get("type"):
                raise ValueError(f"Conflicting report field type: {key}")
            current.pop("description", None)
            if "enum" in current and "enum" in schema:
                current["enum"] = sorted(set(current["enum"]) | set(schema["enum"]))
            else:
                current.pop("enum", None)
            for bound in ("maximum", "maxLength"):
                if bound in schema:
                    current[bound] = max(current.get(bound, schema[bound]), schema[bound])
            for bound in ("minimum", "minLength"):
                if bound in schema:
                    current[bound] = min(current.get(bound, schema[bound]), schema[bound])
    return fields


def _tool(name: str, specs: Mapping[str, RegisteredAgentTool]) -> RegisteredAgentTool:
    comparing = name == COMPARE_NAME
    ids = ("before_report_id", "after_report_id") if comparing else ("report_id",)
    properties = {
        "report_kind": {"type": "string", "enum": sorted(specs),
                        "description": "Kind from the retained report manifest."},
        **{field: {"type": "string", "minLength": 1, "maxLength": 32} for field in ids},
        **_properties(specs),
    }

    description = (
        "Compare two retained reports of the same kind using that kind's page fields."
        if comparing else
        "Read a retained report using its kind's filters and page fields."
    )
    return RegisteredAgentTool(AgentToolDefinition(
        name=name, description=description,
        parameters={"type": "object", "properties": properties,
                    "required": ["report_kind", *ids], "additionalProperties": False},
    ), ReportRouter(name, specs), contract=CapabilityContract(
        entity_fields=tuple((field, "saved_report") for field in ids),
        time_fields=(),
        filter_fields=(
            ()
            if comparing
            else tuple(
                sorted(
                    {
                        field
                        for tool in specs.values()
                        for field in (
                            *(key for key, _ in tool.contract.entity_fields),
                            *tool.contract.time_fields,
                            *tool.contract.filter_fields,
                            *tool.contract.channel_fields,
                        )
                        if field != "report_id"
                    }
                )
            )
        ),
        retained_fields=ids,
        result_paths=tuple(sorted({path for tool in specs.values()
                                   for path in tool.contract.referenceable_result_paths})),
    ))


def replace_report_tools(tools: Sequence[RegisteredAgentTool]) -> tuple[RegisteredAgentTool, ...]:
    by_name = {tool.definition.name: tool for tool in tools}
    readers = {kind: by_name[name] for kind, name in REPORT_READERS.items()}
    comparers = {kind: by_name[name] for kind, name in REPORT_COMPARERS.items()}
    replaced = set(REPORT_READERS.values()) | set(REPORT_COMPARERS.values())
    retained = []
    for tool in tools:
        if tool.definition.name in replaced:
            continue
        description = tool.definition.description
        for previous in REPORT_READERS.values():
            description = description.replace(previous, READ_NAME)
        for previous in REPORT_COMPARERS.values():
            description = description.replace(previous, COMPARE_NAME)
        if description != tool.definition.description:
            tool = replace(tool, definition=replace(tool.definition, description=description))
        retained.append(tool)
    return (*retained, _tool(READ_NAME, readers), _tool(COMPARE_NAME, comparers))
