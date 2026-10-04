"""Bind capability reads to structured entity, time, and access fields."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
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
    entity_fields: tuple[tuple[str, str], ...]
    time_fields: tuple[str, ...]
    scope_field: str | None = None
    scope_variants: tuple[tuple[str, tuple[str, ...]], ...] = ()
    source_scope: str = "other"
    channel_fields: tuple[str, ...] = ()
    result_channel_lists: tuple[tuple[str, str], ...] = ()
    result_channel_fields: tuple[str, ...] = ()
    result_sources_within_query: bool = False
    filter_fields: tuple[str, ...] = ()
    result_entity_keys: tuple[tuple[str, str], ...] = ()
    time_window: tuple[str, str, str] | None = None
    required_access: frozenset[str] = frozenset()
    latest_fields: tuple[str, ...] = ()
    bounded_fields: tuple[str, ...] = ()
    period_results: tuple[tuple[str | int, ...], ...] = ()
    # N denotes a non-negative integer list index; other parts are exact keys.
    result_paths: tuple[tuple[str, ...], ...] = ()
    value_patterns: tuple[tuple[str, str], ...] = ()
    retained_fields: tuple[str, ...] = ()
    # Entity kinds for explicit paths; derived paths inherit result_entity_keys.
    result_path_kinds: tuple[tuple[tuple[str, ...], str], ...] = ()

    @property
    def referenceable_result_paths(self) -> tuple[tuple[str, ...], ...]:
        """Expose entity identities alongside explicitly declared result paths."""
        entity_paths = tuple(
            tuple(field.replace("[]", ".N").split("."))
            for field, _ in self.result_entity_keys
        )
        return tuple(dict.fromkeys((*self.result_paths, *entity_paths)))

    @property
    def referenceable_result_kinds(self) -> dict[tuple[str, ...], str]:
        return {**{tuple(field.replace("[]", ".N").split(".")): entity_kind(kind)
                   for field, kind in self.result_entity_keys},
                **{path: entity_kind(kind) for path, kind in self.result_path_kinds}}

    def result_path_kind(self, path: list[str | int]) -> str | None:
        for pattern, kind in self.referenceable_result_kinds.items():
            if result_path_matches(path, pattern):
                return kind
        return None

    def catalogue_entry(self) -> dict[str, Any]:
        return {
            "entity_keys": dict(self.entity_fields),
            "time_fields": self.time_fields,
            "required_access": tuple(sorted(self.required_access)),
            "scope_variants": dict(self.scope_variants),
            "source_scope": self.source_scope,
            "channel_fields": self.channel_fields,
            "result_channel_lists": self.result_channel_lists,
            "result_channel_fields": self.result_channel_fields,
            "result_sources_within_query": self.result_sources_within_query,
            "filter_fields": self.filter_fields,
            "result_entity_keys": dict(self.result_entity_keys),
            "time_window": self.time_window,
            "latest_fields": self.latest_fields,
            "bounded_fields": self.bounded_fields,
            "period_results": self.period_results,
            "result_paths": self.referenceable_result_paths,
            "result_path_kinds": {"/".join(path): kind for path, kind in self.referenceable_result_kinds.items()},
        }


def result_path_matches(path, pattern) -> bool:
    return len(path) == len(pattern) and all(
        (type(part) is int and part >= 0 or part == "*") if key == "N"
        else type(part) is str and part == key
        for part, key in zip(path, pattern)
    )


def contract_catalogue(registry: Mapping[str, CapabilityTool]) -> dict[str, CapabilityContract]:
    return {name: tool.contract for name, tool in registry.items() if tool.contract is not None}


class CapabilityTool(Protocol):
    definition: AgentToolDefinition
    contract: CapabilityContract | None


def validate_contract_catalogue(registry: Mapping[str, CapabilityTool]) -> None:
    """Catch descriptor drift when a feature changes an exposed query schema."""
    for name, tool in registry.items():
        if tool.definition.name != name:
            raise ValueError(f"Capability registry key differs from its definition: {name}")
        contract = tool.contract
        if contract is None:
            raise ValueError(f"Agent tool lacks capability contract: {name}")
        if not isinstance(contract.required_access, frozenset) or not contract.required_access <= KNOWN_ACCESS_REQUIREMENTS:
            raise ValueError(f"Invalid access requirements in capability contract: {name}")

        def check_unique(labels: tuple[str, ...], category: str) -> None:
            if any(not isinstance(label, str) or not label.strip() for label in labels) or len(
                labels
            ) != len(set(labels)):
                raise ValueError(f"Invalid {category} in capability contract: {name}")

        for category, labels in (
            ("entity fields", tuple(field for field, _ in contract.entity_fields)),
            ("time fields", contract.time_fields),
            ("latest fields", contract.latest_fields),
            ("bounded fields", contract.bounded_fields),
            ("channel fields", contract.channel_fields),
            ("result channel fields", contract.result_channel_fields),
            ("filter fields", contract.filter_fields),
            ("result entity keys", tuple(field for field, _ in contract.result_entity_keys)),
            ("result channel lists", tuple(field for field, _ in contract.result_channel_lists)),
            ("scope variants", tuple(variant for variant, _ in contract.scope_variants)),
        ):
            check_unique(labels, category)
        for category, pairs in (
            ("entity kinds", contract.entity_fields),
            ("result entity kinds", contract.result_entity_keys),
            ("result channel list keys", contract.result_channel_lists),
        ):
            if any(not isinstance(value, str) or not value.strip() for _, value in pairs):
                raise ValueError(f"Invalid {category} in capability contract: {name}")
        if bool(contract.scope_field) != bool(contract.scope_variants):
            raise ValueError(f"Incomplete scope variants in capability contract: {name}")
        if contract.scope_field is not None and not contract.scope_field.strip():
            raise ValueError(f"Invalid scope field in capability contract: {name}")
        for _, selectors in contract.scope_variants:
            check_unique(selectors, "scope selectors")

        fields = set(registry[name].definition.parameters.get("properties", {}))
        described = (
            {field for field, _ in contract.entity_fields}
            | set(contract.time_fields)
            | ({contract.scope_field} if contract.scope_field else set())
            | {field for _, variant in contract.scope_variants for field in variant}
            | set(contract.channel_fields)
            | set(contract.filter_fields)
        )
        if not described <= fields:
            raise ValueError(f"Capability contract and query fields differ: {name}")
        if not set(contract.latest_fields) <= set(contract.time_fields):
            raise ValueError(f"Latest selectors differ from time fields: {name}")
        if not set(contract.retained_fields) <= {field for field, _ in contract.entity_fields}:
            raise ValueError(f"Retained selectors differ from entity fields: {name}")
        if not {field for field, _ in contract.value_patterns} <= fields:
            raise ValueError(f"Value formats differ from query fields: {name}")
        for _, pattern in contract.value_patterns:
            re.compile(pattern)
        if any(not path or any(not (isinstance(part, str) and part or type(part) is int and part >= 0) for part in path)
               for path in contract.period_results):
            raise ValueError(f"Invalid period result path: {name}")
        if any(not isinstance(path, tuple) or not 1 <= len(path) <= 8
               or any(not isinstance(part, str) or not part for part in path)
               for path in contract.result_paths) or len(set(contract.result_paths)) != len(contract.result_paths):
            raise ValueError(f"Invalid result path: {name}")
        if any(not 1 <= len(path) <= 8 or any(not part for part in path)
               for path in contract.referenceable_result_paths):
            raise ValueError(f"Invalid merged result path: {name}")
        annotated = [path for path, _ in contract.result_path_kinds]
        if (any(not isinstance(path, tuple) for path in annotated)
                or len(set(annotated)) != len(annotated)
                or any(path not in contract.result_paths or not isinstance(kind, str) or not kind.strip()
                       for path, kind in contract.result_path_kinds)):
            raise ValueError(f"Invalid result path kind: {name}")
        derived = {tuple(field.replace("[]", ".N").split(".")): entity_kind(kind)
                   for field, kind in contract.result_entity_keys}
        if any(path in derived and derived[path] != entity_kind(kind)
               for path, kind in contract.result_path_kinds):
            raise ValueError(f"Conflicting result path kind: {name}")
        if not set(contract.bounded_fields) <= set(contract.time_fields) or contract.bounded_fields and contract.time_window is None:
            raise ValueError(f"Bounded selectors differ from time fields: {name}")
        if not fields <= described | MECHANICAL_FIELDS:
            raise ValueError(f"Agent query fields lack capability classification: {name}")
        if contract.scope_field is not None:
            scope_schema = registry[name].definition.parameters["properties"][contract.scope_field]
            if set(scope_schema.get("enum", ())) != {
                variant for variant, _ in contract.scope_variants
            }:
                raise ValueError(f"Scope variants differ from query schema: {name}")
        if contract.time_window is not None:
            window = contract.time_window
            if (
                not isinstance(window, tuple) or len(window) != 3
                or window[0] == window[1]
                or window[2] not in {"iso_utc", "unix_seconds"}
                or not set(window[:2]) <= set(contract.time_fields)
            ):
                raise ValueError(f"Invalid time window in capability contract: {name}")
            expected_type = "string" if window[2] == "iso_utc" else "integer"
            properties = registry[name].definition.parameters["properties"]
            if any(properties[field].get("type") != expected_type for field in window[:2]):
                raise ValueError(f"Time window differs from query schema: {name}")
        if contract.source_scope not in {
            "other", "channel_messages", "channel_status", "channel_locator",
            "request_attachment",
            "retained_channel_evidence", "retained_attachment",
            "conversation_control", "request_context",
        }:
            raise ValueError(f"Unknown source-scope kind: {name}")


def bound_time_window(
    contract: CapabilityContract, arguments: Mapping[str, Any],
) -> tuple[datetime | None, datetime | None]:
    """Decode a capability's exact inclusive-start, exclusive-end UTC window."""
    if contract.time_window is None:
        return None, None
    lower_field, upper_field, encoding = contract.time_window

    def decode(field: str) -> datetime | None:
        if field not in arguments:
            return None
        value = arguments[field]
        if encoding == "iso_utc":
            if not isinstance(value, str):
                raise CapabilityBindError("The selected time boundaries must be ISO dates.")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise CapabilityBindError("The selected time boundaries must be ISO dates.") from error
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        if type(value) is not int or value < 0:
            raise CapabilityBindError("The selected end-time boundaries must be UTC Unix seconds.")
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError) as error:
            raise CapabilityBindError("The selected end-time boundaries must be UTC Unix seconds.") from error

    lower, upper = decode(lower_field), decode(upper_field)
    if lower is not None and upper is not None and lower >= upper:
        raise CapabilityBindError(
            "The selected time window must be increasing."
            if encoding == "iso_utc" else
            "The selected end-time window must be increasing."
        )
    return lower, upper


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

    temporal_scope = {
        field: arguments[field]
        for field in contract.time_fields if field in arguments
    }
    for field, pattern in contract.value_patterns:
        if field in arguments and (not isinstance(arguments[field], str) or re.fullmatch(pattern, arguments[field]) is None):
            raise CapabilityBindError(f"The selected {field} does not match {pattern}.")
    bound_time_window(contract, arguments)
    if contract.scope_field:
        selected = arguments.get(contract.scope_field)
        variants = dict(contract.scope_variants)
        if selected not in variants:
            raise CapabilityBindError("The selected scope is unavailable for this metric.")
        expected = set(variants[selected])
        selectors = {field for fields in variants.values() for field in fields}
        if any((field in arguments) != (field in expected) for field in selectors):
            raise CapabilityBindError("The selected scope needs its exact round or war key.")
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
        if identities and all(
            type(known_sources.get(identity)) is int
            and known_sources[identity] > 0 for identity in identities
        ):
            bound_sources = tuple(sorted({
                known_sources[identity] for identity in identities
            }))

    return {
        "precision": "typed_v1",
        "capability": tool.definition.name,
        "entities": entities,
        "temporal_scope": temporal_scope,
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
    "bound_time_window", "compile_capability_call", "require_source_provenance",
    "validate_contract_catalogue",
]
