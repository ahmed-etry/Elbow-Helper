"""Typed, read-only evidence for configured role-connection rules."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any


MAX_ROLE_CONNECTION_RULES = 1_000


@dataclass(frozen=True, slots=True)
class RoleConnectionCondition:
    kind: str
    role_id: int


@dataclass(frozen=True, slots=True)
class RoleConnectionRule:
    index: int
    connection_id: str
    target_role_id: int
    all_conditions: tuple[RoleConnectionCondition, ...]
    any_conditions: tuple[RoleConnectionCondition, ...]
    cyclic: bool


@dataclass(frozen=True, slots=True)
class RoleConnectionSnapshot:
    observed_at: str
    state_fingerprint: str
    total_entries: int
    rules: tuple[RoleConnectionRule, ...]
    malformed_indexes: tuple[int, ...]


def invalid_connection_indexes(
    connections: Sequence[Mapping[str, Any]],
) -> set[int]:
    """Return indexes participating in managed-role dependency cycles."""

    targets: dict[int, set[int]] = {}
    references: dict[int, set[int]] = {}
    for index, connection in enumerate(connections):
        target_role_id = connection.get("target_role_id")
        if isinstance(target_role_id, bool) or not isinstance(target_role_id, int):
            continue
        targets.setdefault(target_role_id, set()).add(index)
        role_references = references.setdefault(target_role_id, set())
        for list_name in ("all", "any"):
            conditions = connection.get(list_name, [])
            if not isinstance(conditions, list):
                continue
            for condition in conditions:
                if not isinstance(condition, dict):
                    continue
                for kind in ("has", "not"):
                    role_id = condition.get(kind)
                    if isinstance(role_id, int) and not isinstance(role_id, bool):
                        role_references.add(role_id)

    managed_roles = set(targets)
    graph = {
        target: role_ids & managed_roles
        for target, role_ids in references.items()
    }

    def reaches_itself(start: int, current: int, visited: set[int]) -> bool:
        for dependency in graph.get(current, set()):
            if dependency == start:
                return True
            if dependency in visited:
                continue
            visited.add(dependency)
            if reaches_itself(start, dependency, visited):
                return True
        return False

    cyclic_targets = {
        target
        for target in managed_roles
        if reaches_itself(target, target, {target})
    }
    return {
        index
        for target in cyclic_targets
        for index in targets.get(target, set())
    }


def connection_matches(
    role_ids: set[int] | frozenset[int], rule: RoleConnectionRule,
) -> bool:
    """Evaluate one valid rule using the feature's existing all/any semantics."""

    for condition in rule.all_conditions:
        if condition.kind == "has" and condition.role_id not in role_ids:
            return False
        if condition.kind == "not" and condition.role_id in role_ids:
            return False
    return not rule.any_conditions or any(
        condition.role_id in role_ids
        if condition.kind == "has" else condition.role_id not in role_ids
        for condition in rule.any_conditions
    )


class RoleConnectionQueries:
    """Snapshot mutable role rules without exposing their state container."""

    def __init__(
        self,
        connection_provider: Callable[[], Sequence[Mapping[str, Any]]],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._connection_provider = connection_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def snapshot(self) -> RoleConnectionSnapshot:
        raw_connections = self._connection_provider()
        if not isinstance(raw_connections, Sequence) or isinstance(
            raw_connections, (str, bytes),
        ):
            raise RuntimeError("Role-connection state is unavailable")
        if len(raw_connections) > MAX_ROLE_CONNECTION_RULES:
            raise RuntimeError("Role-connection state exceeds its read limit")

        copied = tuple(
            dict(connection) if isinstance(connection, Mapping) else connection
            for connection in raw_connections
        )
        cycle_input = tuple(
            connection if isinstance(connection, Mapping) else {}
            for connection in copied
        )
        cyclic = invalid_connection_indexes(cycle_input)
        rules: list[RoleConnectionRule] = []
        malformed: list[int] = []
        for index, raw in enumerate(copied):
            rule = _parse_rule(index, raw, cyclic=index in cyclic)
            if rule is None:
                malformed.append(index)
            else:
                rules.append(rule)

        payload = {
            "total_entries": len(copied),
            "rules": [asdict(rule) for rule in rules],
            "malformed_indexes": malformed,
        }
        fingerprint = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        observed = self._clock()
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return RoleConnectionSnapshot(
            observed.astimezone(timezone.utc).isoformat(), fingerprint,
            len(copied), tuple(rules), tuple(malformed),
        )


def _parse_rule(
    index: int, raw: Any, *, cyclic: bool,
) -> RoleConnectionRule | None:
    if not isinstance(raw, Mapping):
        return None
    connection_id = raw.get("id")
    target_role_id = raw.get("target_role_id")
    if (
        not isinstance(connection_id, str)
        or not connection_id
        or len(connection_id) > 100
        or type(target_role_id) is not int
        or target_role_id <= 0
    ):
        return None
    parsed_lists: list[tuple[RoleConnectionCondition, ...]] = []
    for list_name in ("all", "any"):
        raw_conditions = raw.get(list_name, [])
        if not isinstance(raw_conditions, list) or len(raw_conditions) > 100:
            return None
        conditions: list[RoleConnectionCondition] = []
        for raw_condition in raw_conditions:
            if not isinstance(raw_condition, Mapping) or len(raw_condition) != 1:
                return None
            kind, role_id = next(iter(raw_condition.items()))
            if kind not in {"has", "not"} or type(role_id) is not int or role_id <= 0:
                return None
            conditions.append(RoleConnectionCondition(kind, role_id))
        parsed_lists.append(tuple(conditions))
    return RoleConnectionRule(
        index, connection_id, target_role_id,
        parsed_lists[0], parsed_lists[1], cyclic,
    )


__all__ = [
    "MAX_ROLE_CONNECTION_RULES", "RoleConnectionCondition",
    "RoleConnectionQueries", "RoleConnectionRule", "RoleConnectionSnapshot",
    "connection_matches", "invalid_connection_indexes",
]
