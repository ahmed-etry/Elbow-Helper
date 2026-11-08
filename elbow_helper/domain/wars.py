"""Rules shared by regular and league wars."""

from __future__ import annotations

from typing import Any, Dict


def attacks_per_member(data: Dict[str, Any]) -> int:
    """Attacks each member has in this war; Clash leaves the value out of CWL rounds, which allow one."""
    try:
        value = int(data.get("attacksPerMember"))
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    return 1 if data.get("warTag") or data.get("_warTag") else 2


def normalize_war_state(value: object) -> str:
    return str(value or "").replace("_", "").casefold()

