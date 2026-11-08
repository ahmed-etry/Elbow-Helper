"""Payload conversion and freshness checks for stored war views."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SECOND = 1_000_000


def clash_time(value: int | None) -> str | None:
    """Format epoch microseconds as a Clash timestamp with millisecond precision."""
    if value is None:
        return None
    moment = datetime.fromtimestamp(value // SECOND, timezone.utc)
    return f"{moment:%Y%m%dT%H%M%S}.{value % SECOND // 1000:03d}Z"


def _present(values: dict[str, Any]) -> dict[str, Any]:
    # Clash leaves out what it does not send; the consumers' defaults apply.
    return {key: value for key, value in values.items() if value is not None}


def _side(side: dict[str, Any]) -> dict[str, Any]:
    payload = _present({
        "tag": side["clan_tag"], "name": side["name"], "clanLevel": side["level"],
        "badgeUrls": side["badge"], "stars": side["stars"], "attacks": side["attacks_used"],
        "destructionPercentage": side["destruction"],
    })
    members = [
        _present({
            "tag": member["player_tag"], "name": member["name"],
            "townhallLevel": member["townhall"], "mapPosition": member["map_position"],
            "attacks": [_present({
                "attackerTag": member["player_tag"], "defenderTag": attack["defender_tag"],
                "stars": attack["stars"], "destructionPercentage": attack["destruction"],
                "order": attack["attack_order"], "duration": attack["duration"],
            }) for attack in member["attacks"]],
        })
        for member in side.get("members") or ()
    ]
    if members:
        payload["members"] = members
    return payload


def board_payload(war: dict[str, Any]) -> dict[str, Any]:
    """A record war view, facing its clan, in the shape of Clash's current war."""
    payload = _present({
        "state": war["state"], "teamSize": war["team_size"],
        "attacksPerMember": war["attacks_per_member"],
        "preparationStartTime": clash_time(war["preparation_at"]),
        "startTime": clash_time(war["start_at"]), "endTime": clash_time(war["end_at"]),
        "warTag": war["war_tag"],
    })
    payload["clan"] = _side(war["clan"])
    payload["opponent"] = _side(war["opponent"])
    return payload


def record_fresh(current: dict[str, Any]) -> bool:
    """Whether the latest current-war check is usable now.

    A failed or overdue check is not. A check that stored an incomplete war
    still shows the war as Clash reported it.
    """
    signal = current["coverage"]["signals"][0]
    if current["state"] is None or signal["gaps"]:
        return False
    if signal["status"] == "observed":
        return True
    # Before any complete check, an incomplete one leaves the source "unavailable".
    failures = signal["failures"]
    return (signal["status"] in ("partial", "unavailable") and bool(failures)
            and failures[-1]["at"] == signal["last_attempt_at"] and failures[-1]["status"] == "partial_war")

