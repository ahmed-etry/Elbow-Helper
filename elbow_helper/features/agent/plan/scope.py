"""Carry checked scope through retained reports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..models import AgentRequestContext
from ..capabilities import CONTRACTS
from .checker import _source_check, _time_check


def resource_ids(payload: Any) -> set[str]:
    fields = {field for contract in CONTRACTS.values() for field in contract.retained_fields}
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
    def __init__(self, context: AgentRequestContext):
        self.reports: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for turn in context.state.authorized_history or context.history:
            if turn.record is None:
                continue
            for encoded in turn.record.evidence:
                try:
                    record = json.loads(encoded)
                    result = json.loads(record["result"])
                    for identity in resource_ids(result):
                        self.remember(identity, record["tool"], record["arguments"])
                except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
                    continue

    def remember(self, identity: str, capability: str, arguments: Mapping[str, Any]) -> None:
        contract = CONTRACTS.get(capability)
        if contract is not None and any(field in arguments for field in contract.retained_fields):
            return
        self.reports.setdefault(identity, (capability, dict(arguments)))

    def check(
        self, identities: list[str], periods: tuple, named: Mapping[str, set[str]],
        entities: Mapping[str, set[str]],
    ) -> str:
        for identity in identities:
            origin = self.reports.get(identity)
            if origin is None:
                if periods or named:
                    return "Read this report's source at the declared scope before reusing it."
                continue
            name, arguments = origin
            contract = CONTRACTS.get(name)
            if contract is None:
                return "The retained scope is unavailable."
            issue, _ = _source_check(contract, arguments, named, entities, {})
            if issue:
                return issue
            issue = _time_check(contract, arguments, periods, set())
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
            contract = CONTRACTS.get(name)
            if contract is None:
                continue
            for field in contract.channel_fields:
                value = arguments.get(field)
                result.update(item for item in (value if isinstance(value, list) else [value])
                              if type(item) is int)
        return result
