"""Formatting helpers for connection conditions and summaries."""

from __future__ import annotations

from typing import Any


def _role_mention(role_id: int) -> str:
    return f"<@&{role_id}>"


def _format_condition(condition: dict[str, int]) -> tuple[str, str]:
    if "has" in condition:
        return "has", _role_mention(condition["has"])
    return "not", _role_mention(condition["not"])


def _conditions_to_lines(connection: dict[str, Any]) -> list[str]:
    lines = []
    for cond in connection.get("all", []):
        kind, role = _format_condition(cond)
        if kind == "has":
            lines.append(f"Member has {role}")
        else:
            lines.append(f"Member doesn't have {role}")

    any_conditions = connection.get("any", [])
    if any_conditions:
        kinds = [_format_condition(cond)[0] for cond in any_conditions]
        roles = [_format_condition(cond)[1] for cond in any_conditions]
        if all(kind == "has" for kind in kinds):
            lines.append(
                f"Member has {', '.join(roles[:-1])} or {roles[-1]}"
                if len(roles) > 1
                else f"Member has {roles[0]}"
            )
        elif all(kind == "not" for kind in kinds):
            lines.append(
                f"Member doesn't have {', '.join(roles[:-1])} or {roles[-1]}"
                if len(roles) > 1
                else f"Member doesn't have {roles[0]}"
            )
        else:
            mixed = []
            for cond in any_conditions:
                kind, role = _format_condition(cond)
                label = "has" if kind == "has" else "doesn't have"
                mixed.append(f"{label} {role}")
            lines.append("Member meets any of these conditions: " + ", ".join(mixed))
    return lines


def _removal_conditions_to_lines(connection: dict[str, Any]) -> list[str]:
    lines = []
    all_conditions = connection.get("all", [])
    if all_conditions:
        all_has_roles = []
        all_not_roles = []
        for cond in all_conditions:
            kind, role = _format_condition(cond)
            if kind == "has":
                all_has_roles.append(role)
            else:
                all_not_roles.append(role)
        if all_has_roles:
            if len(all_has_roles) == 1:
                lines.append(f"Member is missing {all_has_roles[0]}")
            else:
                joined = f"{', '.join(all_has_roles[:-1])} or {all_has_roles[-1]}"
                lines.append(f"Member is missing at least one of these roles: {joined}")
        if all_not_roles:
            if len(all_not_roles) == 1:
                lines.append(f"Member has {all_not_roles[0]}")
            else:
                joined = f"{', '.join(all_not_roles[:-1])} or {all_not_roles[-1]}"
                lines.append(f"Member has at least one of these roles: {joined}")

    any_conditions = connection.get("any", [])
    if any_conditions:
        kinds = [_format_condition(cond)[0] for cond in any_conditions]
        roles = [_format_condition(cond)[1] for cond in any_conditions]
        if all(kind == "has" for kind in kinds):
            if len(roles) == 1:
                lines.append(f"Member doesn't have {roles[0]}")
            else:
                joined = f"{', '.join(roles[:-1])} or {roles[-1]}"
                lines.append(f"Member doesn't have any of these roles: {joined}")
        elif all(kind == "not" for kind in kinds):
            if len(roles) == 1:
                lines.append(f"Member has {roles[0]}")
            else:
                joined = f"{', '.join(roles[:-1])} and {roles[-1]}"
                lines.append(f"Member has all of these roles: {joined}")
        else:
            mixed = []
            for cond in any_conditions:
                kind, role = _format_condition(cond)
                label = "has" if kind == "has" else "doesn't have"
                mixed.append(f"{label} {role}")
            lines.append("Member meets none of these conditions: " + ", ".join(mixed))
    return lines


def _conditions_summary(connection: dict[str, Any]) -> str:
    parts = []
    for cond in connection.get("all", []):
        kind, role = _format_condition(cond)
        label = "has" if kind == "has" else "doesn't have"
        parts.append(f"{label} {role}")

    any_conditions = connection.get("any", [])
    if any_conditions:
        any_parts = []
        for cond in any_conditions:
            kind, role = _format_condition(cond)
            label = "has" if kind == "has" else "doesn't have"
            any_parts.append(f"{label} {role}")
        parts.append("(" + " or ".join(any_parts) + ")")
    return " and ".join(parts) if parts else "no conditions"


def _list_label(list_name: str, kind: str) -> str:
    if list_name == "all" and kind == "has":
        return "Has all of these roles"
    if list_name == "all" and kind == "not":
        return "Has none of these roles"
    if list_name == "any" and kind == "has":
        return "Has at least one of these roles"
    return "Is missing at least one of these roles"

