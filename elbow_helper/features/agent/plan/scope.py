"""Carry checked scope through retained reports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..models import AgentRequestContext, RegisteredAgentTool
from ..engine.capability_contract import contract_catalogue
from .checker import source_check, time_check


def resource_ids(payload: Any, registry: Mapping[str, RegisteredAgentTool]) -> set[str]:
    fields = {field for contract in contract_catalogue(registry).values() for field in contract.retained_fields}
    result: set[str] = set()
    def visit(value):
        if isinstance(value, Mapping):
            for field, item in value.items():
                if field in fields:
                    result.update(part for part in (item if isinstance(item, list) else [item])
                                  if isinstance(part, str))
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(payload)
    return result


class ScopeLedger:
    def __init__(self, context: AgentRequestContext, registry: Mapping[str, RegisteredAgentTool]) -> None:
        self.registry = registry
        self.reports: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for turn in context.state.authorized_history or context.history:
            if turn.record is None:
                continue
            for encoded in turn.record.evidence:
                try:
                    record = json.loads(encoded)
                    result = json.loads(record["result"])
                    for identity in resource_ids(result, self.registry):
                        self.remember(identity, record["tool"], record["arguments"])
                except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
                    continue

    def remember(self, identity: str, capability: str, arguments: Mapping[str, Any]) -> None:
        contract = getattr(self.registry.get(capability), "contract", None)
        if contract is not None and any(field in arguments for field in contract.retained_fields):
            return
        if identity not in self.reports:
            self.reports[identity] = (capability, dict(arguments))

    def check(
        self, identities: list[str], periods: tuple, named: Mapping[str, set[str]],
    ) -> str:
        if not periods and not named:
            return ""
        for identity in identities:
            origin = self.reports.get(identity)
            if origin is None:
                if periods or named:
                    return "Read this report's source at the selected scope before reusing it."
                continue
            name, arguments = origin
            contract = getattr(self.registry.get(name), "contract", None)
            if contract is None:
                return "The retained scope is unavailable."
            issue, _ = source_check(contract, arguments, named)
            if issue:
                return issue
            if periods:
                if any(kind == "utc_range" for kind, _, _ in periods) and contract.time_window is None:
                    return "The retained report has no time window for the selected period."
                issue = time_check(contract, arguments, periods, set())
                if issue:
                    return issue
        return ""

    def channels(self, identities: list[str]) -> set[int]:
        result: set[int] = set()
        for identity in identities:
            origin = self.reports.get(identity)
            if origin is None:
                continue
            name, arguments = origin
            contract = getattr(self.registry.get(name), "contract", None)
            if contract is None:
                continue
            for field in contract.channel_fields:
                value = arguments.get(field)
                result.update(item for item in (value if isinstance(value, list) else [value])
                              if type(item) is int)
        return result
