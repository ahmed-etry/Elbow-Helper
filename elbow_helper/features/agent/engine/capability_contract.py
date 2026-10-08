"""Bind capability reads to structured entity, time, and access fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from elbow_helper.domain.player_tags import normalize_player_tag

from ..access import KNOWN_ACCESS_REQUIREMENTS
from elbow_helper.infrastructure.ai import AgentToolDefinition


class CapabilityBindError(ValueError):
    """A structurally valid call does not identify a supported data scope."""


MECHANICAL_FIELDS = frozenset({
    "offset", "limit", "cursor", "page_size", "expected_version",
    "expected_state_fingerprint", "title", "sheets", "report_sheets",
    "written_sheets", "label", "quote", "section_offset",
    "section_limit", "content_offset", "content_limit", "ticket_offset",
    "winner_offset", "query", "report_kind",
})

ENTITY_KINDS = {
    "source_clan": "clan", "destination_clan": "clan",
    "current_clan": "clan", "discord_channel": "discord_channel",
    "parent_discord_channel": "discord_channel",
    "examination_ticket_channel": "discord_channel",
    "recruitment_ticket_channel": "discord_channel",
    "support_ticket_channel": "discord_channel",
}


def entity_kind(kind: str) -> str:
    if kind.endswith("_set"):
        kind = kind[:-4]
    return ENTITY_KINDS.get(kind, kind)


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    entity_fields: tuple[tuple[str, str], ...] = ()
    source_scope: str = "other"
    channel_fields: tuple[str, ...] = ()
    result_channel_lists: tuple[tuple[str, str], ...] = ()
    result_channel_fields: tuple[str, ...] = ()
    result_sources_within_query: bool = False
    filter_fields: tuple[str, ...] = ()
    required_access: frozenset[str] = frozenset()
    retained_fields: tuple[str, ...] = ()

    def catalogue_entry(self):
        from dataclasses import asdict
        return asdict(self)


def contract_catalogue(registry: Mapping[str, CapabilityTool]) -> dict[str, CapabilityContract]:
    return {name: tool.contract for name, tool in registry.items() if tool.contract is not None}


class CapabilityTool(Protocol):
    definition: AgentToolDefinition
    contract: CapabilityContract | None


def validate_contract_catalogue(registry):
    """Check retained access and provenance declarations against their schemas."""
    for name, tool in registry.items():
        if tool.definition.name != name:
            raise ValueError(f"Capability registry key differs from its definition: {name}")
        contract = tool.contract
        if contract is None:
            raise ValueError(f"Agent tool lacks capability contract: {name}")
        if (
            not isinstance(contract.required_access, frozenset)
            or not contract.required_access <= KNOWN_ACCESS_REQUIREMENTS
        ):
            raise ValueError(f"Invalid access requirements in capability contract: {name}")
        fields = set(tool.definition.parameters.get("properties", {}))
        described = (
            {field for field, _ in contract.entity_fields}
            | set(contract.channel_fields) | set(contract.filter_fields)
        )
        if not described <= fields or not set(contract.retained_fields) <= {
            field for field, _ in contract.entity_fields
        }:
            raise ValueError(f"Capability contract and query fields differ: {name}")
        for labels in (
            tuple(field for field, _ in contract.entity_fields), contract.channel_fields,
            contract.filter_fields, contract.retained_fields, contract.result_channel_fields,
        ):
            if len(set(labels)) != len(labels) or any(
                not isinstance(field, str) or not field for field in labels
            ):
                raise ValueError(f"Invalid fields in capability contract: {name}")
        if contract.source_scope not in {
            "other", "channel_messages", "channel_status", "channel_locator",
            "request_attachment",
            "retained_channel_evidence", "retained_attachment",
            "conversation_control", "request_context",
        }:
            raise ValueError(f"Unknown source-scope kind: {name}")


def compile_capability_call(
    tool: CapabilityTool, arguments: Mapping[str, Any],
    known_sources: Mapping[str, int] | None = None,
    *, contract: CapabilityContract | None = None,
) -> dict[str, Any]:
    """Return a bounded, inspectable scope; feature code still owns the calculation."""
    contract = contract or tool.contract
    if contract is None:
        return {"precision": "schema_only", "capability": tool.definition.name}

    entities = []
    for field, kind in contract.entity_fields:
        if field not in arguments:
            continue
        value = arguments[field]
        if kind == "clash_account":
            value = normalize_player_tag(value)
            if value is None:
                raise CapabilityBindError("The account key is not a valid Clash player tag.")
        elif kind == "clash_account_set":
            if not isinstance(value, list):
                raise CapabilityBindError("Account keys must be a list of Clash player tags.")
            normalized = [
                normalize_player_tag(item) if isinstance(item, str) else None
                for item in value
            ]
            if any(item is None for item in normalized):
                raise CapabilityBindError("An account key is not a valid Clash player tag.")
            value = normalized
        entities.append({"kind": kind, "field": field, "key": value})

    bound_sources: tuple[int, ...] = ()
    if contract.source_scope in {
        "retained_channel_evidence", "retained_attachment",
    } and known_sources:
        identities = [
            identity for entity in entities
            if entity["kind"] in {
                "discord_research_job", "discord_research_job_set",
                "discord_research_report", "csv_import", "xlsx_import",
                "text_import", "support_ticket_report",
                "recruitment_trial_report", "examination_case_report",
            }
            for identity in (
                entity["key"] if isinstance(entity["key"], list)
                else [entity["key"]]
            )
        ]
        if identities and all(known_sources.get(identity) for identity in identities):
            bound_sources = tuple(sorted({
                channel for identity in identities
                for channel in (
                    known_sources[identity] if not isinstance(known_sources[identity], int)
                    else (known_sources[identity],)
                )
            }))


    return {
        "precision": "typed_v1",
        "capability": tool.definition.name,
        "entities": entities,
        "predicates": {
            field: arguments[field] for field in contract.filter_fields
            if field in arguments
        },
        "required_access": tuple(sorted(contract.required_access)),
        "source_scope": contract.source_scope,
        "channel_fields": contract.channel_fields,
        "result_channel_lists": contract.result_channel_lists,
        "result_channel_fields": contract.result_channel_fields,
        "result_sources_within_query": contract.result_sources_within_query,
        "bound_source_channels": bound_sources,
    }


def require_source_provenance(
    capability_scope: Mapping[str, Any], arguments: Mapping[str, Any],
    source_channels: set[int], payload: Mapping[str, Any],
) -> None:
    """A successful channel read must bind every explicitly selected source."""
    if capability_scope.get("source_scope") not in {
        "channel_messages", "channel_status", "retained_channel_evidence",
        "request_attachment", "retained_attachment",
    }:
        return
    requested: set[int] = set()
    for field in capability_scope.get("channel_fields", ()):
        value = arguments.get(field)
        if type(value) is int:
            requested.add(value)
        elif isinstance(value, list):
            requested.update(item for item in value if type(item) is int)
    if not requested <= source_channels:
        raise CapabilityBindError("The lookup omitted its requested channel provenance.")
    returned: set[int] = set()
    for field in capability_scope.get("result_channel_fields", ()):
        value = payload.get(field)
        if type(value) is not int:
            raise CapabilityBindError("The lookup returned an unbound source identity.")
        returned.add(value)
    for collection, field in capability_scope.get("result_channel_lists", ()):
        rows = payload.get(collection)
        if not isinstance(rows, list):
            raise CapabilityBindError("The lookup omitted its source-bearing result rows.")
        for row in rows:
            value = row.get(field) if isinstance(row, Mapping) else None
            if type(value) is not int:
                raise CapabilityBindError("The lookup returned an unbound source identity.")
            returned.add(value)
    bound_sources = set(capability_scope.get("bound_source_channels", ()))
    if not returned <= source_channels or (
        bound_sources and not returned <= bound_sources
    ) or (
        requested and capability_scope.get("result_sources_within_query")
        and not returned <= requested
    ):
        raise CapabilityBindError("The lookup returned evidence outside its bound source scope.")


__all__ = [
    "CapabilityContract", "CapabilityTool", "CapabilityBindError", "contract_catalogue",
    "compile_capability_call", "require_source_provenance",
    "validate_contract_catalogue",
]
