"""Clan-promotion intake helpers."""

from __future__ import annotations

from typing import Any
from typing import Dict


PROMO_SOURCES: tuple[str, ...] = ("BEE", "BEC", "BEM", "BE1", "BES", "BE4")

PROMO_ROUTE_MAP: dict[str, tuple[str, ...]] = {
    "BEE": ("BEC", "BEM", "BE1", "BES", "BE4", "BEH"),
    "BEC": ("BEM", "BE1", "BES", "BE4", "BEH"),
    "BEM": ("BE1", "BES", "BE4", "BEH"),
    "BE1": ("BES", "BE4", "BEH"),
    "BES": ("BE4", "BEH"),
    "BE4": ("BEH",),
}

NO_EXAM_TARGETS = {"BEC", "BEM", "BE1"}


def is_valid_route(from_clan: str | None, to_clan: str | None) -> bool:
    if not from_clan or not to_clan:
        return False
    return to_clan in PROMO_ROUTE_MAP.get(from_clan, ())


def valid_targets_for_source(from_clan: str | None) -> tuple[str, ...]:
    if not from_clan:
        return ()
    return PROMO_ROUTE_MAP.get(from_clan, ())


def requires_exam(from_clan: str, to_clan: str) -> bool:
    if not is_valid_route(from_clan, to_clan):
        raise ValueError(f"invalid route {from_clan}->{to_clan}")
    if to_clan in NO_EXAM_TARGETS:
        return False
    if to_clan == "BE4" and from_clan == "BES":
        return False
    return True


def route_summary(from_clan: str | None, to_clan: str | None) -> str | None:
    if not from_clan or not to_clan:
        return None
    return f"{from_clan} -> {to_clan}"


def default_case_fields() -> Dict[str, Any]:
    return {
        "from_clan": None,
        "to_clan": None,
        "route_summary": None,
        "exam_required": None,
        "intake_state": "pending",
        "intake_message_id": None,
        "intake_started_at": None,
        "intake_completed_at": None,
        "intake_last_interaction_at": None,
        "intake_last_reminder_at": None,
        "intake_reminder_count": 0,
        "intake_reminder_message_id": None,
        "th_level_source": None,
    }


def apply_completed_route(case: Dict[str, Any], from_clan: str, to_clan: str) -> None:
    case["from_clan"] = from_clan
    case["to_clan"] = to_clan
    case["route_summary"] = route_summary(from_clan, to_clan)
    case["exam_required"] = requires_exam(from_clan, to_clan)
