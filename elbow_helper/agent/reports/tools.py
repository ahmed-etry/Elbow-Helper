"""Route retained reports through their owning readers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import json
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from ..access import require_evidence_access, require_lookup_access, accessible_message_channel
from ..conversation.context import retained_knowledge_sources


async def authorize_report(context, report_id):
    if (report_id not in context.state.report_sources
            or report_id not in context.state.report_access_requirements):
        return False
    sources = context.state.report_sources.get(report_id, frozenset())
    requirements = context.state.report_access_requirements.get(report_id, frozenset())
    require_lookup_access(context, requirements)
    for channel_id in sources:
        if await accessible_message_channel(context, channel_id) is None:
            return False
    context.state.source_channels.update(sources)
    context.state.required_access.update(requirements)
    for turn in getattr(context, "history", ()):
        if turn.record is not None and any(report_id in encoded for encoded in turn.record.evidence):
            context.state.knowledge_sources.update(retained_knowledge_sources(turn))
    await require_evidence_access(context)
    dependencies = getattr(context, 'read_dependencies', None)
    report = context.state.reports.get(report_id)
    if dependencies is not None and getattr(report, 'family', None) is not None:
        dependencies.inherit(json.loads(report.payload)['summary'].get('source_versions'))
    return True


REPORT_READERS = {
    "discord_research": "read_discord_research_report",
    "achievement_progress": "read_member_achievement_report",
    "event_schedule": "read_event_schedule_report",
    "missing_elder": "read_missing_elder_report",
    "cwl_performance": "read_cwl_performance_report",
    "pending_transfer_requests": "read_pending_transfer_report",
    "member_lifecycle": "read_member_lifecycle_report",
    "active_hibernation": "read_active_hibernation_report",
    "support_ticket_inventory": "read_support_ticket_report",
    "active_recruitment_trials": "read_active_recruitment_trial_report",
    "examination_case_status": "read_examination_case_report",
    "csv_import": "read_csv_import",
    "xlsx_import": "read_xlsx_import",
    "text_import": "read_text_import",
}
REPORT_COMPARERS = {}
READ_NAME = "read_saved_report"
COMPARE_NAME = "compare_saved_reports"


def original_tool(registry: Mapping[str, RegisteredAgentTool], name: str,
                  arguments: Mapping[str, Any]) -> RegisteredAgentTool | None:
    kind = arguments.get("report_kind")
    handler = _router(registry, name)
    if not isinstance(kind, str) or not isinstance(handler, ReportRouter):
        return None
    return handler.specs.get(kind)




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
            report_id = arguments[field]
            report = context.state.reports.get(report_id)
            if report is None or report.manifest().get("kind") != kind:
                return {"error": "That report is unavailable in this conversation."}
            if not await authorize_report(context, report_id):
                return {"error": "That report is unavailable in this conversation."}
        await require_evidence_access(context)
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
        },
        **{field: {"type": "string", "minLength": 1, "maxLength": 32} for field in ids},
        **_properties(specs),
    }
    description = (
        "Compare two retained reports of the same kind using that kind's page fields."
        if comparing else
        "Read a retained report using its kind's filters and page fields."
    )
    return RegisteredAgentTool(
        AgentToolDefinition(
            name=name, description=description,
            usage_note=(
                "Use manifest kind/ID, never 'latest'; keep filters when paging. "
                "XLSX needs exact sheet_name; fields are zero-based "
                "column indexes as strings."
            ),
            parameters={
                "type": "object", "properties": properties,
                "required": ["report_kind", *ids], "additionalProperties": False,
            },
        ),
        ReportRouter(name, specs),
        contract=CapabilityContract(
            entity_fields=tuple((field, "saved_report") for field in ids),
            filter_fields=() if comparing else tuple(sorted({
                field for tool in specs.values()
                for field in (
                    *(key for key, _ in tool.contract.entity_fields),
                    *tool.contract.filter_fields, *tool.contract.channel_fields,
                )
                if field != "report_id"
            })),
            retained_fields=ids,
        ),
    )


def replace_report_tools(tools: Sequence[RegisteredAgentTool]) -> tuple[RegisteredAgentTool, ...]:
    by_name = {tool.definition.name: tool for tool in tools}
    readers = {kind: by_name[name] for kind, name in REPORT_READERS.items() if name in by_name}
    comparers = {kind: by_name[name] for kind, name in REPORT_COMPARERS.items() if name in by_name}
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
    routed = tuple(
        _tool(name, specs)
        for name, specs in ((READ_NAME, readers), (COMPARE_NAME, comparers)) if specs
    )
    return (*retained, *routed)
